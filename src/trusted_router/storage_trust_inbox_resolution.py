"""Audit-only receipts for proven zero-credit PayPal inbox observations.

No credit marker, payment fact, balance, or inbox event is changed here.
Credit and refund continue to share their existing atomic transaction.
"""

from __future__ import annotations

from typing import Any

from trusted_router.services.paypal_inbox import (
    RESOLUTION_KIND,
    RefundedCaptureProof,
    resolution_key,
)
from trusted_router.storage_codec import json_body
from trusted_router.storage_gcp_counter_dml import insert_entity_dml_at
from trusted_router.storage_gcp_trust import _read_payment_tx
from trusted_router.storage_models import TrustInboxRow, Workspace
from trusted_router.trust_tiers import adverse_event_from_payload

REFUND_INDEX_KIND = "trust_paypal_refund"


def retained_refund_row(store: Any, capture_id: str) -> TrustInboxRow | None:
    """Find the retained full-refund evidence by exact indexed keys only."""
    from trusted_router.storage import InMemoryStore
    from trusted_router.storage_gcp import SpannerBigtableStore
    from trusted_router.storage_postgres import PostgresStore

    if isinstance(store, InMemoryStore):
        with store._lock:
            key = store.trust_paypal_refunds.get(capture_id)
            return store.trust_inbox.get(("paypal", key)) if key else None
    if isinstance(store, (SpannerBigtableStore, PostgresStore)):
        pointer = store._read_entity(REFUND_INDEX_KIND, capture_id, dict)
        if pointer is None:
            return None
        key = str(pointer["adverse_ref"])
        if isinstance(store, SpannerBigtableStore):
            with store._database.snapshot() as snapshot:
                records = list(snapshot.execute_sql(
                    "SELECT provider, adverse_ref, payload, received_at FROM tr_trust_inbox "
                    "WHERE provider=@provider AND adverse_ref=@adverse_ref",
                    params={"provider": "paypal", "adverse_ref": key},
                    param_types={"provider": store._param_types.STRING, "adverse_ref": store._param_types.STRING},
                ))
            return TrustInboxRow(*records[0]) if records else None
        with store._pool.connection() as conn:
            record = conn.execute("SELECT provider, adverse_ref, payload, received_at FROM tr_trust_inbox "
                                  "WHERE provider=%s AND adverse_ref=%s", ("paypal", key)).fetchone()
            if record is None:
                return None
            from datetime import datetime

            received = record[3] if isinstance(record[3], datetime) else datetime.fromisoformat(str(record[3]))
            return TrustInboxRow(str(record[0]), str(record[1]), str(record[2]), received)
    raise TypeError("unsupported trust inbox backend")


def record_refunded_uncredited(
    store: Any, proof: RefundedCaptureProof, rows: tuple[TrustInboxRow, ...],
) -> int:
    """Commit receipts only while both payment and credit evidence are absent."""
    from trusted_router.storage import InMemoryStore
    from trusted_router.storage_gcp import SpannerBigtableStore
    from trusted_router.storage_postgres import PostgresStore

    receipts = [(row, proof.receipt(row)) for row in rows]
    events = [adverse_event_from_payload(row.payload) for row, _ in receipts]
    if (proof.amount_micro <= 0 or proof.verified_at.utcoffset() is None
            or not any(event.adverse_ref == proof.refund_ref and event.kind == "refund"
                       and event.lifecycle_status == "succeeded" and event.amount_micro == proof.amount_micro
                       for event in events)):
        raise ValueError("durable refund observation is required")
    refund_row = next(row for row, event in zip(rows, events, strict=True)
                      if event.adverse_ref == proof.refund_ref and event.kind == "refund"
                      and event.lifecycle_status == "succeeded" and event.amount_micro == proof.amount_micro)
    if isinstance(store, InMemoryStore):
        with store._lock:
            if (proof.workspace_id not in store.workspaces
                    or f"paypal_capture:{proof.capture_id}" in store.stripe_events
                    or any(event.provider == "paypal" and event.kind == "payment"
                           and event.original_payment_ref == proof.capture_id
                           for event in store.trust_events.values())):
                return 0
            if any(store.trust_inbox.get((row.provider, row.adverse_ref)) != row for row, _ in receipts):
                return 0
            count = 0
            for row, receipt in receipts:
                key = resolution_key(row)
                if key not in store.trust_inbox_resolutions:
                    store.trust_inbox_resolutions[key] = receipt
                    count += 1
            store.trust_paypal_refunds[proof.capture_id] = refund_row.adverse_ref
            return count
    if isinstance(store, SpannerBigtableStore):
        def spanner_tx(transaction: Any) -> int:
            if (store._read_entity_tx(transaction, "workspace", proof.workspace_id, Workspace) is None
                    or store._read_entity_tx(transaction, "stripe_event",
                                             f"paypal_capture:{proof.capture_id}", dict) is not None
                    or _read_payment_tx(transaction, store._param_types,
                                        provider="paypal", original_payment_ref=proof.capture_id) is not None):
                return 0
            for row, _ in receipts:
                current = list(transaction.execute_sql(
                    "SELECT payload FROM tr_trust_inbox WHERE provider=@provider AND adverse_ref=@adverse_ref",
                    params={"provider": row.provider, "adverse_ref": row.adverse_ref},
                    param_types={"provider": store._param_types.STRING, "adverse_ref": store._param_types.STRING},
                ))
                if len(current) != 1 or current[0][0] != row.payload:
                    return 0
            count = 0
            for row, receipt in receipts:
                key = resolution_key(row)
                if store._read_entity_tx(transaction, RESOLUTION_KIND, key, dict) is None:
                    insert_entity_dml_at(transaction, store._param_types, RESOLUTION_KIND,
                                         key, json_body(receipt), proof.verified_at)
                    count += 1
            if store._read_entity_tx(transaction, REFUND_INDEX_KIND, proof.capture_id, dict) is None:
                insert_entity_dml_at(transaction, store._param_types, REFUND_INDEX_KIND, proof.capture_id,
                                     json_body({"adverse_ref": refund_row.adverse_ref}), proof.verified_at)
            return count
        return int(store._run_in_transaction(spanner_tx))
    if isinstance(store, PostgresStore):
        def postgres_tx(conn: Any) -> int:
            if (store._read_entity_tx(conn, "workspace", proof.workspace_id, Workspace) is None
                    or store._read_entity_tx(conn, "stripe_event", f"paypal_capture:{proof.capture_id}", dict) is not None
                    or conn.execute("SELECT event_id FROM tr_trust_event WHERE provider=%s "
                                    "AND original_payment_ref=%s AND kind='payment'",
                                    ("paypal", proof.capture_id)).fetchone() is not None):
                return 0
            for row, _ in receipts:
                current = conn.execute("SELECT payload FROM tr_trust_inbox "
                                       "WHERE provider=%s AND adverse_ref=%s FOR UPDATE",
                                       (row.provider, row.adverse_ref)).fetchone()
                if current is None or str(current[0]) != row.payload:
                    return 0
            count = sum(store._insert_entity_once_tx(conn, RESOLUTION_KIND, resolution_key(row), receipt)
                        for row, receipt in receipts)
            store._insert_entity_once_tx(conn, REFUND_INDEX_KIND, proof.capture_id,
                                        {"adverse_ref": refund_row.adverse_ref})
            return count
        return int(store._run_transaction(postgres_tx))
    raise TypeError("unsupported trust inbox backend")
