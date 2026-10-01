"""A cached insufficient-credit verdict skips the locking authorize transaction.

A workspace retrying against an empty balance used to run one read-write
transaction per request: lock the credit row with the conditional reserve,
find no headroom, roll back (626k rollbacks on 2026-09-30 from one client).
The first rejection is still transactional; later ones are answered from a
lock-free snapshot that must never refuse a request the transaction accepts.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.test_credit_row_sharding_increment3 import _seed, _typed_authorize
from trusted_router import storage_gcp_authorize as billing
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE

WS = "ws-fragmented"


def _count_transactions(monkeypatch: pytest.MonkeyPatch, database: Any) -> list[int]:
    counter = [0]
    original = database.run_in_transaction

    def counting(*args: Any, **kwargs: Any) -> Any:
        counter[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(database, "run_in_transaction", counting)
    return counter


def _outcome(result: tuple[Any, Any]) -> str:
    return str(result[0])


def test_repeat_rejection_is_answered_without_a_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    store, database, key = _seed([0])
    transactions = _count_transactions(monkeypatch, database)

    first = _typed_authorize(store, key, estimate=100)
    assert _outcome(first) == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    # Positive control: the first rejection really ran the authorize transaction.
    assert transactions[0] >= 1
    assert WS in store._insufficient_credit_workspaces
    after_first = transactions[0]

    second = _typed_authorize(store, key, estimate=100)
    assert _outcome(second) == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert second[1] is None
    assert transactions[0] == after_first
    assert database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["reserved"] == 0


def test_top_up_after_a_cached_rejection_is_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    store, database, key = _seed([0])
    assert _outcome(_typed_authorize(store, key, estimate=100)) == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert WS in store._insufficient_credit_workspaces

    database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["total_credits"] = 1_000
    result = _typed_authorize(store, key, estimate=100)

    assert _outcome(result) == billing.AuthorizeOutcome.ACCEPTED
    assert WS not in store._insufficient_credit_workspaces
    assert database.typed[CREDIT_BALANCE_TABLE][(WS, 0)]["reserved"] == 100


def test_cached_workspace_still_replays_its_own_idempotent_request(monkeypatch: pytest.MonkeyPatch) -> None:
    store, database, key = _seed([150])
    accepted = _typed_authorize(store, key, estimate=100, idempotency_key="idem-1")
    assert _outcome(accepted) == billing.AuthorizeOutcome.ACCEPTED
    # A different request drains what is left and caches the workspace.
    rejected = _typed_authorize(store, key, estimate=100, idempotency_key="idem-2")
    assert _outcome(rejected) == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert WS in store._insufficient_credit_workspaces
    transactions = _count_transactions(monkeypatch, database)

    replay = _typed_authorize(store, key, estimate=100, idempotency_key="idem-1")

    assert _outcome(replay) == billing.AuthorizeOutcome.REPLAY
    assert replay[1] is not None and replay[1].id == accepted[1].id
    # The precheck deferred, so the transaction performed the replay.
    assert transactions[0] >= 1


def test_snapshot_failure_defers_to_the_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    store, database, key = _seed([0])
    assert _outcome(_typed_authorize(store, key, estimate=100)) == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    transactions = _count_transactions(monkeypatch, database)
    original_snapshot = database.snapshot
    precheck_snapshots = [0]

    def failing_snapshot(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("multi_use"):
            precheck_snapshots[0] += 1
            raise RuntimeError("snapshot unavailable")
        return original_snapshot(*args, **kwargs)

    monkeypatch.setattr(database, "snapshot", failing_snapshot)
    result = _typed_authorize(store, key, estimate=100)

    assert precheck_snapshots[0] >= 1
    assert _outcome(result) == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert transactions[0] >= 1


@pytest.mark.parametrize(
    ("totals", "usage", "estimate", "verdict"),
    [
        ([0], [0], 100, billing.EXHAUSTED),
        ([99], [0], 100, billing.EXHAUSTED),
        ([100], [0], 100, billing.HEADROOM),
        # One shard covers the estimate although the aggregate is negative.
        ([10, 0], [0, 20], 5, billing.HEADROOM),
        # No shard covers it alone, but a rebalance could.
        ([3, 3], [0, 0], 5, billing.HEADROOM),
        ([3, 1], [0, 0], 5, billing.EXHAUSTED),
    ],
)
def test_precheck_never_refuses_a_request_a_shard_or_rebalance_could_accept(
    totals: list[int], usage: list[int], estimate: int, verdict: str
) -> None:
    store, database, _key = _seed(totals, usage=usage)
    assert (
        billing.credit_exhaustion_precheck(
            database,
            store._param_types,
            workspace_id=WS,
            estimate=estimate,
            shard_count=len(totals),
        )
        == verdict
    )


def test_a_changed_shard_set_drops_the_entry() -> None:
    store, database, _key = _seed([0, 0])
    assert (
        billing.credit_exhaustion_precheck(
            database, store._param_types, workspace_id=WS, estimate=100, shard_count=3
        )
        == billing.HEADROOM
    )
