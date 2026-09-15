import hashlib
import json
import logging
import time
import uuid
from decimal import Decimal
from typing import Any

import segno

from .credentials import Credentials
from .credits import Credits
from .errors import FundingReviewRequired
from .lnd import Invoice, Lnd
from .money import MAX_CREDIT_RECEIPT, btc, microdollars, usd
from .rates import Rate, Rates
from .store import Store

logger = logging.getLogger("lightning_router")


class Funding:
    def __init__(self, store: Store, credentials: Credentials, lnd: Lnd, rates: Rates, credits: Credits, *, check_capacity: bool = False) -> None:
        self.store = store
        self.credentials = credentials
        self.lnd = lnd
        self.rates = rates
        self.credits = credits
        self.check_capacity = check_capacity
        self._last_health_log = 0.0
        self._last_prune = 0.0

    def account(self, raw_key: str) -> dict[str, Any]:
        key_hash = self.credentials.fingerprint(raw_key)
        account_id = self.credits.resolve(raw_key, new=False)
        self.store.bind_account(key_hash, account_id)
        return {**self.balance(key_hash), "active_invoice": self.store.active(key_hash)}

    def balance(self, key_hash: str) -> dict[str, Any]:
        account_id = self.store.credit_account(key_hash)
        amount = microdollars(self.credits.balance(account_id)) if account_id is not None else 0
        return {"balance_microdollars": str(amount), "balance_usd": usd(amount), "currency": "USD",
                "account_created": account_id is not None}

    def create(self, raw_key: str, request_id: str, cents: int, *, new: bool) -> dict[str, Any]:
        key_hash = self.credentials.fingerprint(raw_key)
        if new:
            self.store.prepare_checkout(key_hash, self.credentials.seal_pending_key(raw_key))
        if not new or self.store.credit_account(key_hash) is not None:
            account_id = self.credits.resolve(raw_key, new=False)
            self.store.bind_account(key_hash, account_id)
        previous = self.store.by_request(key_hash, request_id)
        if previous:
            if previous["usd_cents"] != cents:
                raise ValueError("Idempotency key reused with a different amount")
            return self.refresh(previous)
        rate = self.rates.current()
        requested_msat = rate.invoice_msats(cents)
        if rate.credit_microdollars(requested_msat) > MAX_CREDIT_RECEIPT:
            raise ValueError("Invoice exceeds receipt ceiling")
        if self.check_capacity and self.lnd.receiving_capacity() < requested_msat:
            raise ValueError("Insufficient receiving capacity")
        invoice_id = uuid.uuid4().hex
        preimage = self.credentials.invoice_preimage(invoice_id)
        row = self.store.prepare(
            key_hash, request_id, invoice_id, hashlib.sha256(preimage).hexdigest(),
            int(time.time()), requested_msat=requested_msat,
            usd_cents=cents, usd_per_btc=str(rate.usd_per_btc), fx_margin_bps=rate.fx_margin_bps,
        )
        return self.refresh(row)

    def refresh(self, row: dict[str, Any], *, cancel: bool = False) -> dict[str, Any]:
        row = self.store.invoice(row["id"], row["key_hash"])
        if row["failure_code"] and row["next_attempt_at"] > int(time.time()):
            return self.public(row)
        try:
            return self._refresh(row, cancel=cancel)
        except FundingReviewRequired as exc:
            self.store.failed(row["id"], exc.code, int(time.time()), review=True)
            raise
        except Exception:
            current = self.store.invoice(row["id"], row["key_hash"])
            code = "credit_unavailable" if current["state"] == "SETTLED" else "invoice_unavailable"
            self.store.failed(row["id"], code, int(time.time()), review=False)
            raise

    def _refresh(self, row: dict[str, Any], *, cancel: bool) -> dict[str, Any]:
        if row["state"] == "SETTLED":
            self._deliver_credit(row)
            return self.public(self.store.invoice(row["id"], row["key_hash"]))
        if row["state"] == "CANCELED":
            return self.public(row)
        preimage = self.credentials.invoice_preimage(row["id"])
        # Once published, loss of an LND record is a recovery incident, never
        # authority to issue the payment again (even under the same hash).
        try:
            invoice = (self.lnd.lookup(row["payment_hash"]) if row["bolt11"] else
                       self.lnd.ensure(preimage, row["requested_msat"], expires_at=row["expires_at"]))
        except ValueError as exc:
            raise FundingReviewRequired("invoice_invalid") from exc
        if invoice is None:
            if row["bolt11"]:
                raise FundingReviewRequired("invoice_missing")
            self.store.expire_unissued(row["id"])
            return self.public(self.store.invoice(row["id"], row["key_hash"]))
        if invoice.state == "OPEN" and invoice.expires_at > row["expires_at"]:
            # A delayed LND creation must not extend the fixed quote's lifetime.
            invoice = self.lnd.cancel(row["payment_hash"])
        if cancel and invoice.state not in {"SETTLED", "CANCELED"}:
            invoice = self.lnd.cancel(row["payment_hash"])
        self._observe(row, invoice)
        row = self.store.invoice(row["id"], row["key_hash"])
        self._deliver_credit(row)
        return self.public(self.store.invoice(row["id"], row["key_hash"]))

    def _deliver_credit(self, row: dict[str, Any]) -> None:
        if row["state"] != "SETTLED" or row["credited_at"] is not None:
            return
        if row["credit_microdollars"] > MAX_CREDIT_RECEIPT:
            # Preserve the entire paid value; never silently truncate money.
            raise FundingReviewRequired("credit_amount_limit")
        # No distributed transaction: the settled invoice IS the durable
        # outbox. A crash after remote commit replays the same payment hash.
        checkout = self.store.checkout(row["key_hash"])
        account_id = checkout["credit_account_id"]
        if account_id is None:
            # Only a verified, durably recorded SETTLED invoice may provision.
            # Keep encrypted recovery material until the idempotent identity
            # binding commits so a worker can finish after the browser closes.
            raw_key = self.credentials.open_pending_key(checkout["pending_key"], row["key_hash"])
            try:
                account_id = self.credits.resolve(raw_key, new=True)
            except (KeyError, ValueError) as exc:
                raise FundingReviewRequired("credit_account_unavailable") from exc
            self.store.bind_account(row["key_hash"], account_id)
        try:
            self.credits.credit(account_id, row["payment_hash"], row["credit_microdollars"])
        except FundingReviewRequired:
            raise
        except (KeyError, ValueError) as exc:
            raise FundingReviewRequired("credit_rejected") from exc
        self.store.mark_credited(row["id"], int(time.time()))

    def _observe(self, row: dict[str, Any], invoice: Invoice) -> None:
        if invoice.payment_hash != row["payment_hash"] or invoice.requested_msat != row["requested_msat"]:
            raise FundingReviewRequired("invoice_invalid")
        self.store.attach(row["id"], invoice.bolt11, invoice.expires_at)
        try:
            self.store.observe(
                row["id"], state=invoice.state, payment_hash=invoice.payment_hash,
                amount_msat=invoice.amount_msat, settle_index=invoice.settle_index,
                now=int(time.time()),
            )
        except ValueError as exc:
            raise FundingReviewRequired("invoice_invalid") from exc

    def public(self, row: dict[str, Any]) -> dict[str, Any]:
        rate = Rate(Decimal(row["usd_per_btc"]), row["created_at"], row["fx_margin_bps"])
        return {
            "id": row["id"], "state": row["state"], "expires_at": row["expires_at"],
            "expired": int(time.time()) >= row["expires_at"],
            "usd_amount": f"{row['usd_cents'] // 100}.{row['usd_cents'] % 100:02d}",
            "invoice_btc": btc(row["requested_msat"]),
            "requested_msat": str(row["requested_msat"]),
            "received_msat": str(row["amount_msat"]),
            "credit_microdollars": str(row["credit_microdollars"]),
            "credit_usd": usd(row["credit_microdollars"]),
            "credited": row["credited_at"] is not None,
            "attention_required": row["failure_code"] not in {"", "credit_unavailable", "invoice_unavailable"},
            "bolt11": row["bolt11"],
            "qr": segno.make(row["bolt11"].upper(), error="m").png_data_uri(scale=5, border=4) if row["bolt11"] else None,
            **rate.quote_fields(row["requested_msat"]),
            **self.balance(row["key_hash"]),
        }

    def reconcile(self) -> dict[str, int]:
        counts = {"checked": 0, "failed": 0}
        for row in self.store.pending():
            try:
                self.refresh(row)
                counts["checked"] += 1
            except Exception as exc:
                counts["failed"] += 1
                # Deliberately omit exceptions' text, bearer keys, BOLT11s,
                # preimages and upstream response bodies from durable logs.
                logger.error("lightning.reconcile_failed invoice_id=%s error_type=%s", row["id"], type(exc).__name__)
            finally:
                self.store.checked(row["id"], int(time.time()))
        now = time.monotonic()
        if now - self._last_prune >= 3600:
            self.store.prune(int(time.time()))
            self._last_prune = now
        if now - self._last_health_log >= 60:
            health = self.store.delivery_health(int(time.time()))
            unhealthy = health["review_required"] > 0 or health["oldest_uncredited_seconds"] >= 120
            logger.warning(json.dumps({"severity": "ERROR" if unhealthy else "INFO",
                                       "event": "lightning.funding_health", **health}))
            self._last_health_log = now
        return counts
