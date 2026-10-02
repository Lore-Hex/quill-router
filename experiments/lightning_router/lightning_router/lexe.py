"""Receive-only Lexe adapter. All remote reads use the attesting loopback sidecar."""

import hashlib
import json
import logging
import re
import threading
import time
from decimal import Decimal
from typing import Any

import bolt11
import httpx

from .errors import FundingReviewRequired
from .lnd import Invoice
from .money import msats

SCOPES = {"read_info", "read_payments", "receive"}
PERMISSIONS = {
    "node_info", "list_channels", "get_human_bitcoin_address", "get_payments_by_indexes",
    "get_new_payments", "get_updated_payments", "get_payment_by_id", "list_broadcasted_txs",
    "get_next_unused_address", "create_invoice", "create_offer", "resync", "cancel_payment",
    "get_user_settings",  # Reviewed read_info expansion in Lexe node 0.10.5.
}
INDEX = r"[0-9]{19}-ln_[0-9a-f]{64}"
logger = logging.getLogger("lightning_router")
READ_OPERATIONS = {
    "/v2/node/client_info": "client_info",
    "/v2/node/node_info": "node_info",
    "/v2/node/payment": "payment",
    "/v2/node/updated_payments": "updated_payments",
}
TRANSIENT_READ_ERRORS = (
    httpx.ConnectTimeout, httpx.ReadTimeout, httpx.ConnectError,
    httpx.ReadError, httpx.RemoteProtocolError,
)


class LexeReadinessError(ValueError):
    """A wallet/authority failure, not evidence that an invoice is invalid."""

    def __init__(self, reason: str) -> None:
        messages = {
            "authority_invalid": "Receive-only Lexe authority required",
            "effective_permissions_invalid": "Receive-only Lexe authority required",
            "required_permissions_missing": "Receive-only Lexe authority required",
            "unreviewed_permissions": "Receive-only Lexe authority required",
            "credential_expiry_invalid": "Invalid payment timestamp",
            "credential_expiring": "Lexe credential expired or expiring",
            "wallet_mismatch": "Wrong Lexe wallet",
        }
        if reason not in messages:
            raise ValueError("Unknown Lexe readiness reason")
        self.reason = reason
        super().__init__(messages[reason])


def satoshis(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,3})?", value):
        raise ValueError("Invalid exact satoshi amount")
    return msats(int(Decimal(value) * 1000))


def timestamp(value: Any) -> int:
    if type(value) is not int or not 0 < value < 10**15:
        raise ValueError("Invalid payment timestamp")
    return value


class Lexe:
    def __init__(self, client: httpx.Client, wallet_id: str) -> None:
        if str(client.base_url).rstrip("/") != "http://127.0.0.1:5393" or not re.fullmatch(r"[0-9a-f]{64}", wallet_id):
            raise ValueError("Pinned loopback sidecar and wallet required")
        self.client = client
        self.wallet_id = wallet_id
        self._checked = 0.0
        self._lock = threading.Lock()

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        # Only replay authenticated reads. An ambiguous create/cancel must keep
        # its existing durable recovery path, never become a second mutation.
        if method != "GET" or path not in READ_OPERATIONS:
            return self._request_once(method, path, **kwargs)
        try:
            return self._request_once(method, path, **kwargs)
        except TRANSIENT_READ_ERRORS as exc:
            logger.warning("lightning.lexe_read_retry operation=%s error_type=%s attempt=1",
                           READ_OPERATIONS[path], type(exc).__name__)
        retry_kwargs = {**kwargs, "timeout": httpx.Timeout(5, connect=1, pool=1)}
        try:
            result = self._request_once(method, path, **retry_kwargs)
        except TRANSIENT_READ_ERRORS as exc:
            logger.error("lightning.lexe_read_failed operation=%s error_type=%s attempts=2",
                         READ_OPERATIONS[path], type(exc).__name__)
            raise
        logger.warning("lightning.lexe_read_recovered operation=%s attempts=2", READ_OPERATIONS[path])
        return result

    def _request_once(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with self.client.stream(method, path, follow_redirects=False, **kwargs) as response:
            # Do not propagate bodies, invoice secrets or diagnostic text to logs.
            if response.status_code == 404 and path == "/v2/node/payment":
                raise FundingReviewRequired("invoice_missing")
            if response.status_code != 200:
                raise RuntimeError("Lexe request unavailable")
            body = bytearray()
            for chunk in response.iter_bytes():
                body.extend(chunk)
                if len(body) > 2_097_152:
                    raise ValueError("Lexe response too large")
        result = json.loads(body)
        if not isinstance(result, dict):
            raise ValueError("Invalid Lexe response")
        return result

    def ready(self) -> None:
        with self._lock:
            if self._checked and time.monotonic() - self._checked < 60:
                return
            try:
                self._verify_ready()
            except LexeReadinessError as exc:
                logger.error("lightning.lexe_readiness_failed reason=%s", exc.reason)
                raise
            self._checked = time.monotonic()

    def _verify_ready(self) -> None:
        info = self.request("GET", "/v2/node/client_info")
        scopes, permissions, effective = info.get("scopes"), info.get("permissions", []), info.get("effective_permissions")
        if (info.get("kind") != "client_credentials" or not isinstance(scopes, list)
                or not all(isinstance(s, str) for s in scopes) or set(scopes) != SCOPES or permissions != []):
            raise LexeReadinessError("authority_invalid")
        if not isinstance(effective, list) or not all(isinstance(p, str) for p in effective):
            raise LexeReadinessError("effective_permissions_invalid")
        if not {"node_info", "get_payment_by_id", "get_updated_payments", "create_invoice", "cancel_payment"} <= set(effective):
            raise LexeReadinessError("required_permissions_missing")
        if not set(effective) <= PERMISSIONS:
            raise LexeReadinessError("unreviewed_permissions")
        try:
            expires = timestamp(info.get("expires_at"))
        except ValueError as exc:
            raise LexeReadinessError("credential_expiry_invalid") from exc
        if expires <= int(time.time() * 1000) + 3_600_000:
            raise LexeReadinessError("credential_expiring")
        node = self.request("GET", "/v2/node/node_info")
        if node.get("user_pk") != self.wallet_id:
            raise LexeReadinessError("wallet_mismatch")

    @staticmethod
    def note(row: dict[str, Any]) -> str:
        return "lightningrouter:" + row["id"]

    def parse(self, data: dict[str, Any], row: dict[str, Any]) -> Invoice:
        if row["wallet_id"] != self.wallet_id:
            raise ValueError("Wrong invoice wallet")
        index, payment_hash, encoded = data.get("index"), data.get("hash"), data.get("invoice")
        if (not isinstance(index, str) or not re.fullmatch(INDEX, index)
                or not isinstance(payment_hash, str) or index[23:] != payment_hash
                or row["payment_hash"] not in {None, payment_hash}
                or row["provider_index"] not in {"", index}
                or data.get("rail") != "invoice" or data.get("kind") != "invoice"
                or data.get("direction") != "inbound" or data.get("personal_note") != self.note(row)
                or not isinstance(encoded, str) or len(encoded) > 8192):
            raise ValueError("Invalid Lexe invoice identity")
        try:
            decoded = bolt11.decode(encoded)
        except Exception as exc:
            raise ValueError("Invalid signed invoice") from exc
        created = timestamp(data.get("created_at"))
        expires = timestamp(data.get("expires_at")) // 1000
        if (not decoded.is_mainnet() or decoded.payment_hash != payment_hash
                or decoded.amount_msat != row["requested_msat"] or decoded.expiry_time != expires):
            raise ValueError("Invoice terms differ from quote")
        # Lexe assigns created_at on first persist, after signing the invoice.
        # Validate ordering/freshness, not equality across two clock reads.
        if (int(index[:19]) != created
                or not row["created_at"] - 30 <= decoded.date <= created // 1000
                or created >= expires * 1000):
            raise FundingReviewRequired("invoice_timestamp_invalid")
        state = {"pending": "OPEN", "completed": "SETTLED", "failed": "CANCELED"}.get(str(data.get("status")))
        if state is None:
            raise ValueError("Unknown Lexe payment state")
        amount, fee = satoshis(data.get("amount")), satoshis(data.get("fees"))
        finalized = 0
        if state == "SETTLED":
            preimage = data.get("preimage")
            if (not isinstance(preimage, str) or not re.fullmatch(r"[0-9a-f]{64}", preimage)
                    or hashlib.sha256(bytes.fromhex(preimage)).hexdigest() != payment_hash):
                raise ValueError("Payment proof missing or invalid")
            finalized = timestamp(data.get("finalized_at"))
            if finalized < created or amount <= 0:
                raise ValueError("Invalid settlement")
            # Lexe reports net receipt. Preserve gross quote credit and record
            # the skimmed fee separately, with exact millisatoshi arithmetic.
            amount = msats(amount + fee)
            if amount < row["requested_msat"] or amount > 2 * row["requested_msat"]:
                raise ValueError("Receipt outside invoice limits")
        else:
            if amount != row["requested_msat"] or fee != 0:
                raise ValueError("Pending invoice amount changed")
            amount = 0
        return Invoice(payment_hash, encoded, state, amount, finalized, expires,
                       row["requested_msat"], index, fee)

    def lookup(self, row: dict[str, Any]) -> Invoice:
        self.ready()
        data = self.request("GET", "/v2/node/payment", params={"index": row["provider_index"]})
        return self.parse(data, row)

    def create(self, row: dict[str, Any]) -> Invoice:
        self.ready()
        remaining = min(870, row["expires_at"] - int(time.time()) - 30)
        if remaining <= 0:
            raise FundingReviewRequired("creation_expired")
        data = self.request("POST", "/v2/node/create_invoice", json={
            "amount": str(Decimal(row["requested_msat"]) / 1000), "expiration_secs": remaining,
            "description": "LightningRouter API credit", "personal_note": self.note(row),
        })
        index = data.get("index")
        if not isinstance(index, str) or not re.fullmatch(INDEX, index):
            raise ValueError("Invalid created invoice index")
        # Always verify by a fresh authenticated payment read before publishing.
        return self.lookup({**row, "provider_index": index})

    def recover(self, row: dict[str, Any]) -> Invoice | None:
        self.ready()
        # Read remote updates, not sidecar's possibly incomplete local cache.
        # Bound recovery to ten pages after the intent time, never full history.
        cursor = f"u{max(0, row['created_at'] - 60) * 1000:019d}-ln_{'0' * 64}"
        matches: dict[str, dict[str, Any]] = {}
        for _ in range(10):
            page = self.request("GET", "/v2/node/updated_payments", params={"start_index": cursor, "limit": 100})
            payments = page.get("payments")
            if not isinstance(payments, list) or len(payments) > 100:
                raise ValueError("Invalid recovery page")
            for item in payments:
                if not isinstance(item, dict):
                    raise ValueError("Invalid recovery payment")
                if item.get("personal_note") == self.note(row):
                    parsed = self.parse(item, row)
                    matches[parsed.provider_index] = item
            if len(payments) < 100:
                if not matches and time.time() - row["create_started_at"] < 60:
                    raise RuntimeError("Invoice creation in progress")
                if not matches:
                    return None  # Only a completed authoritative scan proves absence.
                if len(matches) != 1:
                    raise FundingReviewRequired("creation_ambiguous")
                index = next(iter(matches))
                return self.lookup({**row, "provider_index": index})
            next_cursor = page.get("updated_index")
            if not isinstance(next_cursor, str) or not re.fullmatch("u?" + INDEX, next_cursor) or next_cursor == cursor:
                raise ValueError("Invalid recovery cursor")
            cursor = next_cursor
        raise FundingReviewRequired("creation_recovery_limit")

    def cancel(self, row: dict[str, Any]) -> Invoice:
        self.ready()
        try:
            self.request("POST", "/v2/node/cancel_payment", json={"index": row["provider_index"]})
        except (RuntimeError, httpx.HTTPError):
            pass  # A racing receipt may have won; only authoritative state decides.
        invoice = self.lookup(row)
        if invoice.state not in {"SETTLED", "CANCELED"}:
            raise RuntimeError("Invoice cancellation pending")
        return invoice
