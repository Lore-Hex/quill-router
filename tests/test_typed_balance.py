from __future__ import annotations

from trusted_router.storage import InMemoryStore
from trusted_router.typed_balance import live_credit_summary


def test_live_credit_summary_memory_store_uses_credit_money_book() -> None:
    store = InMemoryStore()
    workspace = store.create_workspace(
        owner_user_id="typed-balance-user",
        name="Balance",
        trial_credit_microdollars=0,
    )
    _raw_key, api_key = store.create_api_key(
        workspace_id=workspace.id,
        name="balance",
        creator_user_id="typed-balance-user",
    )

    assert store.credit_workspace_once(workspace.id, 2_000_000, "evt_balance") is True
    reservation = store.reserve(workspace.id, api_key.hash, 750_000)

    assert live_credit_summary(workspace.id, store=store) == {
        "total_credits": 2_000_000,
        "total_usage": 0,
        "reserved": 750_000,
        "available": 1_250_000,
    }

    store.settle(reservation.id, 500_000)

    assert live_credit_summary(workspace.id, store=store) == {
        "total_credits": 2_000_000,
        "total_usage": 500_000,
        "reserved": 0,
        "available": 1_500_000,
    }


def test_live_credit_summary_memory_store_missing_account_returns_none() -> None:
    store = InMemoryStore()
    assert live_credit_summary("missing", store=store) is None
