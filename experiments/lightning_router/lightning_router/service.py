import hashlib
import logging
import time
import uuid
from typing import Any

import segno

from .credentials import Credentials
from .lnd import Invoice, Lnd
from .money import btc
from .rates import Rates
from .store import Store

logger = logging.getLogger("lightning_router")


class Funding:
    def __init__(self, store: Store, credentials: Credentials, lnd: Lnd, rates: Rates) -> None:
        self.store = store
        self.credentials = credentials
        self.lnd = lnd
        self.rates = rates

    def create(self, raw_key: str, request_id: str, cents: int, *, new: bool) -> dict[str, Any]:
        key_hash = self.credentials.fingerprint(raw_key)
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
            int(time.time()), new=new, requested_msat=rate.invoice_msats(cents),
            usd_cents=cents, usd_per_btc=str(rate.usd_per_btc),
        )
        return self.refresh(row)

    def refresh(self, row: dict[str, Any], *, cancel: bool = False) -> dict[str, Any]:
        if row["state"] in {"SETTLED", "CANCELED"}:
            return self.public(row)
        preimage = self.credentials.invoice_preimage(row["id"])
        invoice = self.lnd.ensure(preimage, row["requested_msat"])
        if cancel and invoice.state not in {"SETTLED", "CANCELED"}:
            invoice = self.lnd.cancel(row["payment_hash"])
        self._observe(row, invoice)
        return self.public(self.store.invoice(row["id"], row["key_hash"]))

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
            "bolt11": row["bolt11"],
            "qr": segno.make(row["bolt11"].upper(), error="m").png_data_uri(scale=5, border=4) if row["bolt11"] else None,
            **self.store.balance(row["key_hash"]),
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
