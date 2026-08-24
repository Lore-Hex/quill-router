from __future__ import annotations

import json
import random
import threading
import time
from collections import Counter
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome, authorize_atomic
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_credit_shards import (
    CreditShardCountCache,
    randomized_credit_shards,
)
from trusted_router.storage_models import CreditAccount


def _credit_row(
    workspace_id: str,
    shard: int,
    total_credits: int,
) -> dict[str, object]:
    return {
        "workspace_id": workspace_id,
        "shard": shard,
        "total_credits": total_credits,
        "total_usage": 0,
        "reserved": 0,
        "source_updated_at": None,
        "updated_at": None,
    }


def _seed(
    totals: list[int],
    *,
    workspace_id: str = "ws-sharded",
    key_limit: int | None = None,
) -> tuple[Any, Any, Any]:
    store, database, _ = make_fake_store()
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id, shard_count=len(totals)),
    )
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for shard, total in enumerate(totals):
        table[(workspace_id, shard)] = _credit_row(workspace_id, shard, total)
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="shard-test",
        creator_user_id=None,
        limit_microdollars=key_limit,
    )
    return store, database, key


def _auth_body(authorization_id: str, reservation_id: str) -> str:
    return json.dumps(
        {"id": authorization_id, "credit_reservation_id": reservation_id}
    )


def _authorize(
    store: Any,
    *,
    workspace_id: str,
    key_hash: str,
    estimate: int,
    candidates: tuple[int, ...],
    scope: str | None = None,
) -> dict[str, Any]:
    return authorize_atomic(
        store._database,
        store._param_types,
        workspace_id=workspace_id,
        key_hash=key_hash,
        estimate=estimate,
        has_credit_candidate=True,
        reservation_usage_type="Credits",
        idempotency_scope=scope,
        idempotency_fingerprint="same-body" if scope else None,
        expires_at="2026-12-01T00:00:00Z",
        build_auth_body=_auth_body,
        credit_shard_candidates=candidates,
    )


def test_randomized_order_is_complete_unique_and_spreads_first_choice() -> None:
    first_choices = Counter(
        randomized_credit_shards(8, rng=random.Random(seed))[0]  # noqa: S311 - deterministic
        for seed in range(256)
    )

    assert set(first_choices) == set(range(8))
    assert all(count >= 20 for count in first_choices.values())
    for seed in range(32):
        order = randomized_credit_shards(8, rng=random.Random(seed))  # noqa: S311
        assert len(order) == 8
        assert set(order) == set(range(8))


def test_one_shard_order_does_not_touch_rng() -> None:
    class ExplodingRandom(random.Random):
        def sample(self, population: Any, k: int, *, counts: Any = None) -> list[Any]:
            raise AssertionError("one-shard path must not call RNG")

    assert randomized_credit_shards(1, rng=ExplodingRandom()) == (0,)


def test_invalid_or_unbounded_candidate_configuration_fails_closed() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        randomized_credit_shards(65)

    store, _database, key = _seed([100])
    common = {
        "database": store._database,
        "param_types": store._param_types,
        "workspace_id": "ws-sharded",
        "key_hash": key.hash,
        "estimate": 1,
        "has_credit_candidate": True,
        "reservation_usage_type": "Credits",
        "idempotency_scope": None,
        "idempotency_fingerprint": None,
        "expires_at": "2026-12-01T00:00:00Z",
        "build_auth_body": _auth_body,
    }
    with pytest.raises(ValueError, match="must not be empty"):
        authorize_atomic(**common, credit_shard_candidates=())
    with pytest.raises(ValueError, match="must be unique"):
        authorize_atomic(**common, credit_shard_candidates=(0, 0))
    with pytest.raises(ValueError, match="non-negative"):
        authorize_atomic(**common, credit_shard_candidates=(-1,))


def test_scan_skips_insufficient_shard_and_records_first_success() -> None:
    store, database, key = _seed([100, 1_000], key_limit=2_000)

    result = _authorize(
        store,
        workspace_id="ws-sharded",
        key_hash=key.hash,
        estimate=500,
        candidates=(0, 1),
    )

    assert result["outcome"] == AuthorizeOutcome.ACCEPTED
    assert result["credit_shard"] == 1
    reservation = database.reservations[result["reservation_id"]]
    assert reservation["credit_shard"] == 1
    assert database.typed[CREDIT_BALANCE_TABLE][("ws-sharded", 0)]["reserved"] == 0
    assert database.typed[CREDIT_BALANCE_TABLE][("ws-sharded", 1)]["reserved"] == 500
    assert database.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] == 500


def test_all_shards_insufficient_rolls_back_key_hold_and_writes_nothing() -> None:
    store, database, key = _seed([100, 200, 300], key_limit=10_000)

    result = _authorize(
        store,
        workspace_id="ws-sharded",
        key_hash=key.hash,
        estimate=500,
        candidates=(2, 0, 1),
    )

    assert result == {"outcome": AuthorizeOutcome.INSUFFICIENT_CREDITS}
    assert database.reservations == {}
    assert database.typed[KEY_LIMIT_TABLE][(key.hash, 0)]["reserved"] == 0
    assert all(
        row["reserved"] == 0
        for row in database.typed[CREDIT_BALANCE_TABLE].values()
    )


def test_replay_keeps_original_shard_when_candidate_order_changes() -> None:
    store, database, key = _seed([1_000, 1_000, 1_000])

    first = _authorize(
        store,
        workspace_id="ws-sharded",
        key_hash=key.hash,
        estimate=250,
        candidates=(2, 1, 0),
        scope="stable-replay",
    )
    replay = _authorize(
        store,
        workspace_id="ws-sharded",
        key_hash=key.hash,
        estimate=250,
        candidates=(0, 1, 2),
        scope="stable-replay",
    )

    assert first["outcome"] == AuthorizeOutcome.ACCEPTED
    assert replay["outcome"] == AuthorizeOutcome.REPLAY
    assert replay["credit_shard"] == first["credit_shard"] == 2
    assert len(database.reservations) == 1
    assert database.typed[CREDIT_BALANCE_TABLE][("ws-sharded", 2)]["reserved"] == 250
    assert database.typed[CREDIT_BALANCE_TABLE][("ws-sharded", 0)]["reserved"] == 0


def test_shard_count_cache_hits_expires_and_is_bounded() -> None:
    now = [100.0]
    cache = CreditShardCountCache(
        ttl_seconds=10,
        max_entries=2,
        clock=lambda: now[0],
    )
    loads = Counter()

    def load(name: str, value: int) -> int:
        loads[name] += 1
        return value

    assert cache.get("a", lambda: load("a", 2)) == 2
    assert cache.get("a", lambda: load("a", 3)) == 2
    assert loads["a"] == 1

    now[0] = 111
    assert cache.get("a", lambda: load("a", 3)) == 3
    assert loads["a"] == 2

    assert cache.get("b", lambda: load("b", 1)) == 1
    assert cache.get("c", lambda: load("c", 1)) == 1
    assert cache.get("a", lambda: load("a", 4)) == 4  # evicted LRU
    assert loads["a"] == 3


def test_shard_count_cache_singleflights_same_workspace_miss() -> None:
    cache = CreditShardCountCache(ttl_seconds=60, max_entries=100)
    load_count = 0
    load_lock = threading.Lock()
    start = threading.Barrier(9)
    results: list[int] = []

    def loader() -> int:
        nonlocal load_count
        with load_lock:
            load_count += 1
        time.sleep(0.02)
        return 16

    def worker() -> None:
        start.wait(timeout=5)
        results.append(cache.get("hot-workspace", loader))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    start.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert results == [16] * 8
    assert load_count == 1


def test_store_byok_path_does_not_load_credit_shard_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _database, key = _seed([100, 100])

    def fail_if_called(_workspace_id: str) -> tuple[int, ...]:
        raise AssertionError("BYOK authorize must not load credit shard configuration")

    monkeypatch.setattr(store, "_credit_shard_candidates", fail_if_called)
    outcome, authorization = store.authorize_gateway_typed(
        workspace_id="ws-sharded",
        key_hash=key.hash,
        estimate=10,
        has_credit_candidate=False,
        reservation_usage_type="BYOK",
        model_id="model",
        provider="provider",
        requested_model_id=None,
        candidate_model_ids=["model"],
        region="us",
        endpoint_id="endpoint",
        candidate_endpoint_ids=["endpoint"],
        idempotency_key=None,
        idempotency_fingerprint=None,
    )

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None


def test_concurrent_sharded_reserves_never_exceed_global_cap() -> None:
    shard_count = 8
    per_shard_credit = 40
    estimate = 10
    store, database, key = _seed([per_shard_credit] * shard_count)
    start = threading.Barrier(65)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def worker(index: int) -> None:
        order = randomized_credit_shards(
            shard_count,
            rng=random.Random(index),  # noqa: S311 - deterministic test distribution
        )
        start.wait(timeout=10)
        result = _authorize(
            store,
            workspace_id="ws-sharded",
            key_hash=key.hash,
            estimate=estimate,
            candidates=order,
            scope=f"request-{index}",
        )
        with outcomes_lock:
            outcomes.append(result["outcome"])

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(64)]
    for thread in threads:
        thread.start()
    start.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()

    capacity = shard_count * per_shard_credit // estimate
    assert outcomes.count(AuthorizeOutcome.ACCEPTED) == capacity
    assert outcomes.count(AuthorizeOutcome.INSUFFICIENT_CREDITS) == 64 - capacity
    rows = database.typed[CREDIT_BALANCE_TABLE]
    assert sum(row["reserved"] for row in rows.values()) == capacity * estimate
    assert all(
        row["reserved"] + row["total_usage"] <= row["total_credits"]
        for row in rows.values()
    )
    assert len(database.reservations) == capacity
