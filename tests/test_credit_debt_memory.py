"""The memory store's one balance takes money in debt-first (fast-admission design section 4.7).

A negative balance is repaid before unrecovered payment claims are offered
anything, as the Spanner store's credit rows are (tests/test_credit_debt_spanner.py).
"""

from __future__ import annotations

from dataclasses import fields
from datetime import UTC, datetime

from trusted_router.storage import InMemoryStore
from trusted_router.storage_models import CreditProvenance, TrustEvent

NOW = datetime(2026, 10, 7, tzinfo=UTC)


def _store_with(headroom: int, claim: int) -> tuple[InMemoryStore, str]:
    store = InMemoryStore()
    workspace = store.create_workspace(owner_user_id="owner", name="debt")
    money = store.credit_money[workspace.id]
    money.total_credits_microdollars = 100
    money.total_usage_microdollars = 100 - headroom
    money.reserved_microdollars = 0
    values = dict.fromkeys(field.name for field in fields(TrustEvent))
    values.update(
        workspace_id=workspace.id, event_id="claim", kind="payment", provider="stripe",
        occurred_at=NOW, recorded_at=NOW, unrecovered_micro=claim, recovered_micro=0,
        recovery_target=claim, debit_status="unrecovered",
    )
    store.trust_events[(workspace.id, "claim")] = TrustEvent(**values)
    return store, workspace.id


def _headroom(store: InMemoryStore, workspace_id: str) -> int:
    money = store.credit_money[workspace_id]
    return (
        money.total_credits_microdollars
        - money.total_usage_microdollars
        - money.reserved_microdollars
    )


def test_shard_debt_comes_before_payment_claims() -> None:
    store, workspace_id = _store_with(headroom=-50, claim=100)
    assert store.credit_workspace_typed_direct(
        workspace_id, 50, "evt-repay", provenance=CreditProvenance.system_grant(),
    ) is True
    assert _headroom(store, workspace_id) == 0
    assert store.trust_events[(workspace_id, "claim")].unrecovered_micro == 100


def test_what_repays_the_debt_is_then_offered_to_claims() -> None:
    store, workspace_id = _store_with(headroom=-50, claim=50)
    assert store.credit_workspace_typed_direct(
        workspace_id, 100, "evt-repay-and-claim", provenance=CreditProvenance.system_grant(),
    ) is True
    assert _headroom(store, workspace_id) == 0
    assert store.trust_events[(workspace_id, "claim")].unrecovered_micro == 0
