from __future__ import annotations

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE


@pytest.mark.parametrize("creator", ["email", "wallet", "workspace"])
def test_new_small_workspace_uses_one_credit_shard(creator: str) -> None:
    store, database = make_fake_store()
    if creator == "workspace":
        workspace = store.create_workspace("owner", "small balance")
    else:
        user = (
            store.ensure_user("starter@example.com", trial_credit_microdollars=0)
            if creator == "email"
            else store.create_wallet_user("0x" + "a" * 40)
        )
        workspace = store.list_workspaces_for_user(user.id)[0]
    account = store.get_credit_account(workspace.id)
    assert account is not None and account.shard_count == 1
    assert [key for key in database.typed[CREDIT_BALANCE_TABLE] if key[0] == workspace.id] == [
        (workspace.id, 0),
    ]


def test_starter_burst_reserves_affordable_requests_without_rebalancing(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import storage_gcp_credit_rebalance as rebalance

    store, database = make_fake_store()
    workspace = store.create_workspace("owner", "starter", trial_credit_microdollars=300_000)
    _, key = store.create_api_key(workspace_id=workspace.id, name="uncapped", creator_user_id="owner")

    def unexpected_rebalance(*args: object, **kwargs: object) -> None:
        raise AssertionError("starter admission must not rebalance fragmented credit")

    monkeypatch.setattr(rebalance, "rebalance_credit_for_estimate", unexpected_rebalance)
    outcomes = []
    for _ in range(4):
        outcome, authorization = store.authorize_gateway_typed(
            workspace_id=workspace.id, key_hash=key.hash, estimate=100_000,
            has_credit_candidate=True, reservation_usage_type="Credits", model_id="model",
            provider="provider", requested_model_id=None, candidate_model_ids=[], region=None,
            endpoint_id=None, candidate_endpoint_ids=[], idempotency_key=None,
            idempotency_fingerprint=None,
        )
        outcomes.append(outcome)
        assert (authorization is not None) == (outcome == AuthorizeOutcome.ACCEPTED)
    assert outcomes == [AuthorizeOutcome.ACCEPTED] * 3 + [AuthorizeOutcome.INSUFFICIENT_CREDITS]
    rows = [row for (ws, _), row in database.typed[CREDIT_BALANCE_TABLE].items() if ws == workspace.id]
    assert sum(row["total_credits"] for row in rows) == 300_000
    assert sum(row["reserved"] for row in rows) == 300_000
    assert sum(row["total_usage"] for row in rows) == 0
    assert key.budget_strict is False
