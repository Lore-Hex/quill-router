"""PayPal/Adyen inbox replay on the existing PostgreSQL credit transaction."""
from __future__ import annotations

from typing import Any

from trusted_router.trust_tiers import adverse_event_from_payload


def drain_provider_inbox_tx(store: Any, conn: Any, provider: str, payment_ref: str | None) -> None:
    if provider not in {"paypal", "adyen"} or not payment_ref:
        return
    rows = conn.execute(
        "SELECT adverse_ref, payload FROM tr_trust_inbox WHERE provider=%s "
        "ORDER BY received_at, adverse_ref FOR UPDATE", (provider,),
    ).fetchall()
    observations = [(str(key), adverse_event_from_payload(str(payload))) for key, payload in rows]
    for key, event in sorted(observations, key=lambda row: (row[1].provider_ordering_watermark, row[0])):
        if event.original_payment_ref != payment_ref:
            continue
        result = store.record_adverse_trust_event(event, _connection=conn)
        if result.outcome == "inbox":
            raise RuntimeError("Provider inbox still cannot resolve its payment")
        deleted = conn.execute(
            "DELETE FROM tr_trust_inbox WHERE provider=%s AND adverse_ref=%s",
            (provider, key),
        )
        if deleted.rowcount != 1:
            raise RuntimeError("Provider inbox drain lost its row guard")
