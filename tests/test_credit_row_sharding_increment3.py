from __future__ import annotations

import threading
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome, settle_atomic
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_credit_rebalance import (
    RebalanceOutcome,
    rebalance_credit_for_estimate,
)
from trusted_router.storage_gcp_credit_shards import CreditShardCountCache
from trusted_router.storage_models import CreditAccount


def _seed(
    totals: list[int],
    *,
    usage: list[int] | None = None,
    reserved: list[int] | None = None,
    workspace_id: str = "ws-fragmented",
) -> tuple[Any, Any, Any]:
    store, database, _ = make_fake_store()
    usage = usage or [0] * len(totals)
    reserved = reserved or [0] * len(totals)
    assert len(usage) == len(totals)
    assert len(reserved) == len(totals)
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(
            workspace_id=workspace_id,
            total_credits_microdollars=sum(totals),
            total_usage_microdollars=sum(usage),
            reserved_microdollars=sum(reserved),
            shard_count=len(totals),
        ),
    )
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard, total in enumerate(totals):
        table[(workspace_id, shard)] = {
            "workspace_id": workspace_id,
            "shard": shard,
            "total_credits": total,
            "total_usage": usage[shard],
            "reserved": reserved[shard],
            "source_updated_at": None,
            "updated_at": None,
        }
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="fragmentation-test",
        creator_user_id=None,
        limit_microdollars=None,
    )
    return store, database, key


def _rebalance(
    store: Any,
    *,
    shard_count: int,
    target_shard: int,
    estimate: int,
    workspace_id: str = "ws-fragmented",
) -> dict[str, int | str]:
    return rebalance_credit_for_estimate(
        store._database,
        store._param_types,
        workspace_id=workspace_id,
        shard_count=shard_count,
        target_shard=target_shard,
        estimate=estimate,
    )


def _typed_authorize(
    store: Any,
    key: Any,
    *,
    estimate: int,
    idempotency_key: str | None = None,
) -> tuple[str, Any]:
    return store.authorize_gateway_typed(
        workspace_id="ws-fragmented",
        key_hash=key.hash,
        estimate=estimate,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        model_id="model",
        provider="provider",
        requested_model_id=None,
        candidate_model_ids=["model"],
        region="us",
        endpoint_id="endpoint",
        candidate_endpoint_ids=["endpoint"],
        idempotency_key=idempotency_key,
        idempotency_fingerprint="same-body" if idempotency_key else None,
    )


def test_rebalance_moves_only_idle_budget_and_preserves_global_totals() -> None:
    store, database, _key = _seed([100, 100], usage=[60, 60])

    result = _rebalance(store, shard_count=2, target_shard=0, estimate=60)

    assert result == {
        "outcome": RebalanceOutcome.MOVED,
        "moved_micro": 20,
        "target_shard": 0,
    }
    rows = database.typed[CREDIT_BALANCE_TABLE]
    assert rows[("ws-fragmented", 0)]["total_credits"] == 120
    assert rows[("ws-fragmented", 1)]["total_credits"] == 80
    assert [rows[("ws-fragmented", shard)]["total_usage"] for shard in range(2)] == [60, 60]
    assert sum(row["total_credits"] for row in rows.values()) == 200
    assert all(row["reserved"] == 0 for row in rows.values())


def test_rebalance_can_consolidate_from_multiple_donors() -> None:
    store, database, _key = _seed([100, 100, 100, 100], usage=[80, 80, 80, 80])

    result = _rebalance(store, shard_count=4, target_shard=0, estimate=70)

    assert result["outcome"] == RebalanceOutcome.MOVED
    assert result["moved_micro"] == 50
    rows = database.typed[CREDIT_BALANCE_TABLE]
    assert rows[("ws-fragmented", 0)]["total_credits"] == 150
    assert sum(row["total_credits"] for row in rows.values()) == 400
    assert all(row["total_credits"] >= row["total_usage"] + row["reserved"] for row in rows.values())


def test_rebalance_distinguishes_not_needed_insufficient_and_incomplete() -> None:
    store, database, _key = _seed([100, 100], usage=[20, 60])
    before = {
        key: dict(value)
        for key, value in database.typed[CREDIT_BALANCE_TABLE].items()
    }

    assert _rebalance(store, shard_count=2, target_shard=0, estimate=60)[
        "outcome"
    ] == RebalanceOutcome.NOT_NEEDED
    assert _rebalance(store, shard_count=2, target_shard=1, estimate=130)[
        "outcome"
    ] == RebalanceOutcome.INSUFFICIENT
    assert database.typed[CREDIT_BALANCE_TABLE] == before

    database.typed[CREDIT_BALANCE_TABLE].pop(("ws-fragmented", 1))
    assert _rebalance(store, shard_count=2, target_shard=0, estimate=90)[
        "outcome"
    ] == RebalanceOutcome.INCOMPLETE


def test_rebalance_fails_closed_on_preexisting_sub_budget_violation() -> None:
    store, database, _key = _seed([100, 100], usage=[101, 0])
    before = {
        key: dict(value)
        for key, value in database.typed[CREDIT_BALANCE_TABLE].items()
    }

    with pytest.raises(RuntimeError, match="exceeds its sub-budget"):
        _rebalance(store, shard_count=2, target_shard=0, estimate=50)

    assert database.typed[CREDIT_BALANCE_TABLE] == before


def test_store_rebalances_fragmentation_then_authorizes_and_settles_once() -> None:
    store, database, key = _seed([100, 100], usage=[60, 60])

    outcome, authorization = _typed_authorize(store, key, estimate=60)

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    [reservation] = database.reservations.values()
    selected_shard = reservation["credit_shard"]
    assert database.typed[CREDIT_BALANCE_TABLE][
        ("ws-fragmented", selected_shard)
    ]["reserved"] == 60
    assert sum(
        row["total_credits"]
        for row in database.typed[CREDIT_BALANCE_TABLE].values()
    ) == 200

    settled = settle_atomic(
        store._database,
        store._param_types,
        reservation_id=reservation["reservation_id"],
        actual_micro=55,
        settled_usage_type="Credits",
        success=True,
    )
    assert settled["outcome"] == "settled"
    assert sum(
        row["total_usage"]
        for row in database.typed[CREDIT_BALANCE_TABLE].values()
    ) == 175
    assert sum(
        row["reserved"]
        for row in database.typed[CREDIT_BALANCE_TABLE].values()
    ) == 0


def test_true_insufficient_store_authorize_rolls_back_every_hold() -> None:
    store, database, key = _seed([100, 100], usage=[70, 70])

    outcome, authorization = _typed_authorize(store, key, estimate=70)

    assert outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert authorization is None
    assert database.reservations == {}
    assert database.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] == 0
    assert sum(
        row["total_credits"]
        for row in database.typed[CREDIT_BALANCE_TABLE].values()
    ) == 200


def test_stale_smaller_cache_refreshes_and_uses_newly_activated_shards() -> None:
    store, database, key = _seed([0, 0, 100])
    store._credit_shard_counts = CreditShardCountCache(ttl_seconds=600)
    assert store._credit_shard_counts.get("ws-fragmented", lambda: 1) == 1

    outcome, authorization = _typed_authorize(store, key, estimate=50)

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    [reservation] = database.reservations.values()
    assert reservation["credit_shard"] == 2


def test_stale_larger_cache_refreshes_after_rejection_without_drift_error() -> None:
    store, _database, key = _seed([40])
    store._credit_shard_counts = CreditShardCountCache(ttl_seconds=600)
    assert store._credit_shard_counts.get("ws-fragmented", lambda: 3) == 3

    outcome, authorization = _typed_authorize(store, key, estimate=60)

    assert outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert authorization is None


def test_persistent_missing_configured_shard_raises_and_leaks_no_hold() -> None:
    store, database, key = _seed([100, 100, 100], usage=[60, 60, 60])
    database.typed[CREDIT_BALANCE_TABLE].pop(("ws-fragmented", 2))

    with pytest.raises(RuntimeError, match="configured credit shard set is incomplete"):
        _typed_authorize(store, key, estimate=60)

    assert database.reservations == {}
    assert database.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] == 0


def test_concurrent_fragmentation_repair_preserves_cap_and_accepts_at_most_one() -> None:
    store, database, key = _seed([100, 100], usage=[60, 60])
    start = threading.Barrier(3)
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        start.wait(timeout=10)
        outcome, _authorization = _typed_authorize(
            store,
            key,
            estimate=60,
            idempotency_key=f"concurrent-{index}",
        )
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    start.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()

    assert outcomes.count(AuthorizeOutcome.ACCEPTED) == 1
    assert outcomes.count(AuthorizeOutcome.INSUFFICIENT_CREDITS) == 1
    rows = database.typed[CREDIT_BALANCE_TABLE]
    assert sum(row["total_credits"] for row in rows.values()) == 200
    assert sum(row["reserved"] for row in rows.values()) == 60
    assert all(
        row["total_usage"] + row["reserved"] <= row["total_credits"]
        for row in rows.values()
    )
    assert len(database.reservations) == 1
