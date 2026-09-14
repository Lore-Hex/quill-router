import hashlib
import logging
import time
import uuid
from typing import Any

import segno

from .credentials import Credentials
from .credits import Credits
from .lnd import Invoice, Lnd
from .money import btc, microdollars, usd
from .rates import Rates
from .store import Store

logger = logging.getLogger("lightning_router")


class Funding:
    def __init__(self, store: Store, credentials: Credentials, lnd: Lnd, rates: Rates, credits: Credits) -> None:
        self.store = store
        self.credentials = credentials
        self.lnd = lnd
        self.rates = rates
        self.credits = credits

    def account(self, raw_key: str) -> dict[str, Any]:
        key_hash = self.credentials.fingerprint(raw_key)
        account_id = self.credits.resolve(raw_key, new=False)
        self.store.bind_account(key_hash, account_id)
        return {**self.balance(key_hash), "active_invoice": self.store.active(key_hash)}

    def balance(self, key_hash: str) -> dict[str, str]:
        amount = microdollars(self.credits.balance(self.store.credit_account(key_hash)))
        return {"balance_microdollars": str(amount), "balance_usd": usd(amount), "currency": "USD"}

    def create(self, raw_key: str, request_id: str, cents: int, *, new: bool) -> dict[str, Any]:
        key_hash = self.credentials.fingerprint(raw_key)
        account_id = self.credits.resolve(raw_key, new=new)
        self.store.bind_account(key_hash, account_id)
        previous = self.store.by_request(key_hash, request_id)
        if previous:
            if previous["usd_cents"] != cents:
                raise ValueError("Idempotency key reused with a different amount")
            return self.refresh(previous)
        rate = self.rates.current()
        invoice_id = uuid.uuid4().hex
        preimage = self.credentials.invoice_preimage(invoice_id)
        row = self.store.prepare(
            key_hash, request_id, invoice_id, hashlib.sha256(preimage).hexdigest(),
            int(time.time()), requested_msat=rate.invoice_msats(cents),
            usd_cents=cents, usd_per_btc=str(rate.usd_per_btc),
        )
        return self.refresh(row)

    def refresh(self, row: dict[str, Any], *, cancel: bool = False) -> dict[str, Any]:
        row = self.store.invoice(row["id"], row["key_hash"])
        if row["state"] == "SETTLED":
            self._deliver_credit(row)
            return self.public(self.store.invoice(row["id"], row["key_hash"]))
        if row["state"] == "CANCELED":
            return self.public(row)
        preimage = self.credentials.invoice_preimage(row["id"])
        invoice = self.lnd.ensure(preimage, row["requested_msat"], expires_at=row["expires_at"])
        if invoice is None:
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
        # No distributed transaction: the settled invoice IS the durable
        # outbox. A crash after remote commit replays the same payment hash.
        self.credits.credit(self.store.credit_account(row["key_hash"]),
                            row["payment_hash"], row["credit_microdollars"])
        self.store.mark_credited(row["id"], int(time.time()))

    def _observe(self, row: dict[str, Any], invoice: Invoice) -> None:
        if invoice.payment_hash != row["payment_hash"] or invoice.requested_msat != row["requested_msat"]:
            raise ValueError("Invoice binding changed")
        self.store.attach(row["id"], invoice.bolt11, invoice.expires_at)
        self.store.observe(
            row["id"], state=invoice.state, payment_hash=invoice.payment_hash,
            amount_msat=invoice.amount_msat, settle_index=invoice.settle_index,
            now=int(time.time()),
        )

    def public(self, row: dict[str, Any]) -> dict[str, Any]:
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
            "bolt11": row["bolt11"],
            "qr": segno.make(row["bolt11"].upper(), error="m").png_data_uri(scale=5, border=4) if row["bolt11"] else None,
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
        return counts
