"""Provider inbox drain using the existing Postgres writer in the credit tx."""
from __future__ import annotations

from typing import Any

from trusted_router.storage_models import CreditProvenance, TrustEvent
from trusted_router.storage_trust_reconciliation import PostgresTrustReconciliationRepository
from trusted_router.trust_tiers import adverse_event_from_payload


def drain_provider_inbox_tx(store: Any, conn: Any, provider: str, payment_ref: str) -> None:
    if provider not in {"paypal", "adyen"}:
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


# The historical writer records provenance without issuing credits. It still
# must drain an adverse observation accepted before that provenance existed.
def provider_reconciliation_repository(store: Any) -> Any:
    from trusted_router.storage_trust_reconciliation import (
        PostgresTrustReconciliationRepository,
        trust_reconciliation_repository,
    )

    repository = trust_reconciliation_repository(store)
    if not isinstance(repository, PostgresTrustReconciliationRepository):
        return repository
    return ProviderPostgresReconciliationRepository(repository.store)


class ProviderPostgresReconciliationRepository(PostgresTrustReconciliationRepository):
    def write_payment_fact(self, event: TrustEvent) -> bool:
        if event.provider not in {"paypal", "adyen"}:
            return super().write_payment_fact(event)

        def write(conn: Any) -> bool:
            inserted = self.store._insert_credit_trust_event_tx(
                conn, workspace_id=event.workspace_id, event_id=event.event_id,
                amount_microdollars=int(event.credited_micro or 0),
                provenance=CreditProvenance(str(event.provider_subtype), event.provider,
                                            event.original_payment_ref, event.occurred_at),
                recorded_at=event.recorded_at,
                payment_amount_microdollars=event.payment_amount_micro, currency=event.currency,
            )
            if event.original_payment_ref is not None:
                drain_provider_inbox_tx(self.store, conn, event.provider, event.original_payment_ref)
            return bool(inserted)

        return bool(self.store._run_transaction(write))
