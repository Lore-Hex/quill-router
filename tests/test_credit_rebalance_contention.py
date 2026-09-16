from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_errors import StoreUnavailable
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_credit_rebalance import (
    RebalanceOutcome,
    credit_headroom_precheck,
    rebalance_precheck,
)
from trusted_router.storage_gcp_credit_shards import (
    REFRESH_MIN_INTERVAL_SECONDS,
    CreditShardCountCache,
)
from trusted_router.storage_models import CreditAccount

WORKSPACE_ID = "ws-contention"


def _seed(
    totals: list[int],
    *,
    usage: list[int] | None = None,
    reserved: list[int] | None = None,
    workspace_id: str = WORKSPACE_ID,
) -> tuple[Any, Any, Any]:
    store, database, _ = make_fake_store()
    usage = usage or [0] * len(totals)
    reserved = reserved or [0] * len(totals)
    assert len(usage) == len(totals)
    assert len(reserved) == len(totals)
    store._write_entity(
        "credit",
        workspace_id,
        CreditAccount(workspace_id=workspace_id, shard_count=len(totals)),
    )
    _set_credit_rows(database, totals, usage=usage, reserved=reserved, workspace_id=workspace_id)
    _raw, key = store.api_keys.create(
        workspace_id=workspace_id,
        name="contention-test",
        creator_user_id=None,
        limit_microdollars=None,
    )
    return store, database, key


def _set_credit_rows(
    database: Any,
    totals: list[int],
    *,
    usage: list[int] | None = None,
    reserved: list[int] | None = None,
    workspace_id: str = WORKSPACE_ID,
) -> None:
    usage = usage or [0] * len(totals)
    reserved = reserved or [0] * len(totals)
    table = database.typed.setdefault(CREDIT_BALANCE_TABLE, {})
    for key in [key for key in table if key[0] == workspace_id]:
        table.pop(key)
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


def _typed_authorize(
    store: Any,
    key: Any,
    *,
    estimate: int,
    workspace_id: str = WORKSPACE_ID,
    idempotency_key: str | None = None,
) -> tuple[str, Any]:
    return store.authorize_gateway_typed(
        workspace_id=workspace_id,
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


def _typed_rows(database: Any, workspace_id: str = WORKSPACE_ID) -> dict[tuple, dict[str, Any]]:
    return {
        key: dict(value)
        for key, value in database.typed[CREDIT_BALANCE_TABLE].items()
        if key[0] == workspace_id
    }


def _install_rebalance_spy(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

    calls = {"count": 0}
    original = rebalance_mod.rebalance_credit_for_estimate

    def spy(*args: Any, **kwargs: Any) -> dict[str, int | str]:
        calls["count"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(rebalance_mod, "rebalance_credit_for_estimate", spy)
    return calls


def test_true_exhaustion_skips_rw_rebalance(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _database, key = _seed([100, 100], usage=[70, 70])
    calls = _install_rebalance_spy(monkeypatch)

    outcome, authorization = _typed_authorize(store, key, estimate=70)

    assert outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert authorization is None
    assert calls["count"] == 0


def test_true_exhaustion_bounds_conditional_credit_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A depleted tenant must not lock every configured credit shard."""
    from trusted_router import storage_gcp_authorize as authorize_mod

    store, _database, key = _seed([100] * 16, usage=[100] * 16)
    attempts: list[tuple[int, ...]] = []
    original = authorize_mod.authorize_atomic

    def spy(*args: Any, **kwargs: Any) -> dict[str, Any]:
        candidates = tuple(kwargs["credit_shard_candidates"])
        attempts.append(candidates)
        return original(*args, **kwargs)

    monkeypatch.setattr(authorize_mod, "authorize_atomic", spy)

    outcome, authorization = _typed_authorize(store, key, estimate=1)

    assert outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert authorization is None
    assert attempts
    assert max(len(candidates) for candidates in attempts) <= 4


def test_bounded_attempts_repair_headroom_outside_hot_subset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bounding the write set must not turn aggregate funds into a false 402."""
    totals = [0] * 16
    totals[7] = 100
    store, database, key = _seed(totals)
    candidates = tuple(range(16))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _workspace_id: candidates)
    monkeypatch.setattr(
        store,
        "_refresh_credit_shard_candidates",
        lambda _workspace_id: candidates,
    )

    calls = _install_rebalance_spy(monkeypatch)

    outcome, authorization = _typed_authorize(store, key, estimate=80)
    followup_outcome, followup_authorization = _typed_authorize(store, key, estimate=20)

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    assert followup_outcome == AuthorizeOutcome.ACCEPTED
    assert followup_authorization is not None
    rows = database.typed[CREDIT_BALANCE_TABLE]
    assert rows[(WORKSPACE_ID, 0)]["reserved"] == 0
    assert rows[(WORKSPACE_ID, 7)]["reserved"] == 100
    assert rows[(WORKSPACE_ID, 7)]["total_credits"] == 100
    assert calls["count"] == 0


def test_fragmented_sufficient_still_rebalances_and_accepts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _database, key = _seed([100, 100], usage=[60, 60])
    calls = _install_rebalance_spy(monkeypatch)

    outcome, authorization = _typed_authorize(store, key, estimate=60)

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    assert calls["count"] == 1


@pytest.mark.parametrize(
    ("totals", "usage", "target_shard", "estimate", "expected"),
    [
        ([0, 60, 40, 0], [0, 0, 0, 40], 0, 100, RebalanceOutcome.INSUFFICIENT),
        ([100, 100], [20, 60], 0, 60, RebalanceOutcome.NOT_NEEDED),
        ([100, 100], [60, 60], 0, 60, RebalanceOutcome.MOVED),
    ],
)
def test_rebalance_precheck_verdicts_are_read_only(
    totals: list[int],
    usage: list[int],
    target_shard: int,
    estimate: int,
    expected: str,
) -> None:
    store, database, _key = _seed(totals, usage=usage)
    before = _typed_rows(database)

    verdict = rebalance_precheck(
        store._database,
        store._param_types,
        workspace_id=WORKSPACE_ID,
        shard_count=len(totals),
        target_shard=target_shard,
        estimate=estimate,
    )

    assert verdict == expected
    assert _typed_rows(database) == before


def test_rebalance_precheck_incomplete_and_nonpositive_are_read_only() -> None:
    store, database, _key = _seed([100, 100], usage=[60, 60])
    database.typed[CREDIT_BALANCE_TABLE].pop((WORKSPACE_ID, 1))
    before = _typed_rows(database)

    incomplete = rebalance_precheck(
        store._database,
        store._param_types,
        workspace_id=WORKSPACE_ID,
        shard_count=2,
        target_shard=0,
        estimate=60,
    )
    nonpositive = rebalance_precheck(
        store._database,
        store._param_types,
        workspace_id=WORKSPACE_ID,
        shard_count=2,
        target_shard=0,
        estimate=0,
    )

    assert incomplete == RebalanceOutcome.INCOMPLETE
    assert nonpositive == RebalanceOutcome.INCOMPLETE
    assert _typed_rows(database) == before


def test_zero_estimate_precheck_selects_a_viable_later_shard() -> None:
    """A zero estimate must not blindly select an overdrawn prefix shard."""
    store, database, _key = _seed(
        [100, 100, 100, 100, 100, 100],
        usage=[101, 101, 101, 101, 100, 99],
    )
    before = _typed_rows(database)

    precheck = credit_headroom_precheck(
        store._database,
        store._param_types,
        workspace_id=WORKSPACE_ID,
        shard_count=6,
        shard_candidates=tuple(range(6)),
        estimate=0,
    )

    assert precheck.outcome == RebalanceOutcome.NOT_NEEDED
    assert precheck.candidate_shard == 4
    assert _typed_rows(database) == before


def test_authoritative_rebalance_exhaustion_returns_402(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A locked all-shard verdict is proof, not a retryable ambiguity."""
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

    store, _database, key = _seed([100, 100], usage=[60, 60])
    monkeypatch.setattr(
        rebalance_mod,
        "credit_headroom_precheck",
        lambda *_args, **_kwargs: rebalance_mod.CreditHeadroomPrecheck(
            RebalanceOutcome.MOVED
        ),
    )
    monkeypatch.setattr(
        rebalance_mod,
        "rebalance_credit_for_estimate",
        lambda *_args, **_kwargs: {
            "outcome": RebalanceOutcome.INSUFFICIENT,
            "moved_micro": 0,
            "target_shard": 0,
        },
    )

    outcome, authorization = _typed_authorize(store, key, estimate=60)

    assert outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert authorization is None


def test_rebalance_cooldown_returns_retryable_error_not_false_402(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

    store, database, key = _seed([100, 100], usage=[60, 60])
    calls = _install_rebalance_spy(monkeypatch)
    monkeypatch.setattr(rebalance_mod, "REBALANCE_COOLDOWN_SECONDS", 60.0)

    first_outcome, first_authorization = _typed_authorize(
        store,
        key,
        estimate=60,
        idempotency_key="first",
    )
    assert first_outcome == AuthorizeOutcome.ACCEPTED
    assert first_authorization is not None
    assert calls["count"] == 1

    database.reservations.clear()
    database.reservation_idemp.clear()
    _set_credit_rows(database, [100, 100], usage=[60, 60])

    with pytest.raises(StoreUnavailable, match="rebalance is busy"):
        _typed_authorize(
            store,
            key,
            estimate=60,
            idempotency_key="second",
        )
    assert calls["count"] == 1

    monkeypatch.setattr(rebalance_mod, "REBALANCE_COOLDOWN_SECONDS", 0.0)
    _set_credit_rows(database, [100, 100], usage=[60, 60])

    third_outcome, third_authorization = _typed_authorize(
        store,
        key,
        estimate=60,
        idempotency_key="third",
    )
    assert third_outcome == AuthorizeOutcome.ACCEPTED
    assert third_authorization is not None
    assert calls["count"] == 2


def test_repeated_headroom_races_return_retryable_error_not_false_402(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bounded retry limit is not evidence that aggregate funds are gone."""
    from trusted_router import storage_gcp_authorize as authorize_mod

    store, _database, key = _seed([100, 100])

    def always_stolen(*_args: Any, **_kwargs: Any) -> dict[str, str]:
        return {"outcome": AuthorizeOutcome.INSUFFICIENT_CREDITS}

    monkeypatch.setattr(authorize_mod, "authorize_atomic", always_stolen)

    with pytest.raises(StoreUnavailable, match="changed concurrently"):
        _typed_authorize(store, key, estimate=50)


@pytest.mark.parametrize("peer_result", ["funded", "exhausted", "still_busy"])
def test_cooldown_wait_rechecks_without_another_rebalance(
    monkeypatch: pytest.MonkeyPatch, peer_result: str,
) -> None:
    """A peer repair may finish milliseconds after the initial read-only check."""
    from trusted_router import storage_gcp as gcp
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

    store, database, key = _seed([100, 100], usage=[60, 60])
    candidates = (0, 1)
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_credit_rebalance_cooldown_allows", lambda _ws: False)
    original_rebalance = rebalance_mod.rebalance_credit_for_estimate
    calls = _install_rebalance_spy(monkeypatch)
    waits: list[float] = []

    def peer_finishes(seconds: float) -> None:
        waits.append(seconds)
        # The real transaction runner has returned before sleeping. No current
        # authorization exists yet, so this wait cannot double-charge on replay.
        assert not database.reservations
        if peer_result == "funded":
            original_rebalance(
                database, store._param_types, workspace_id=WORKSPACE_ID,
                shard_count=2, target_shard=0, estimate=60,
            )
        elif peer_result == "exhausted":
            _set_credit_rows(database, [100, 100], usage=[100, 100])

    monkeypatch.setattr(gcp.time, "sleep", peer_finishes)
    if peer_result == "still_busy":
        with pytest.raises(StoreUnavailable, match="rebalance is busy"):
            _typed_authorize(store, key, estimate=60)
        assert len(waits) == 2
        assert not database.reservations
        assert sum(row["reserved"] for row in _typed_rows(database).values()) == 0
    else:
        outcome, authorization = _typed_authorize(
            store, key, estimate=60, idempotency_key="waiting-request",
        )
        assert len(waits) == 1
        if peer_result == "funded":
            assert outcome == AuthorizeOutcome.ACCEPTED
            assert authorization is not None
            assert _typed_authorize(
                store, key, estimate=60, idempotency_key="waiting-request",
            )[0] == AuthorizeOutcome.REPLAY
            rows = _typed_rows(database).values()
            assert sum(row["total_credits"] for row in rows) == 200
            assert sum(row["total_usage"] for row in rows) == 120
            assert sum(row["reserved"] for row in rows) == 60
        else:
            assert outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS
            assert authorization is None
            assert not database.reservations
    assert calls["count"] == 0
    assert 0 < sum(waits) <= 0.5


def test_cooldown_expiry_permits_one_guarded_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router import storage_gcp as gcp

    store, database, key = _seed([100, 100], usage=[60, 60])
    now = [100.0]
    monkeypatch.setattr(gcp.time, "monotonic", lambda: now[0])
    waits: list[float] = []

    def advance(seconds: float) -> None:
        waits.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(gcp.time, "sleep", advance)
    assert store._credit_rebalance_cooldown_allows(WORKSPACE_ID)
    calls = _install_rebalance_spy(monkeypatch)
    outcome, authorization = _typed_authorize(store, key, estimate=60)
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    assert sum(waits) == 0.5
    assert calls["count"] == 1
    assert sum(row["reserved"] for row in _typed_rows(database).values()) == 60


def test_reject_path_refresh_dedupes_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _database, key = _seed([100, 100], usage=[70, 70])
    original_factory = store._credit_shard_count_loader
    loads = {"count": 0}

    def counted_factory(workspace_id: str) -> Any:
        original_loader = original_factory(workspace_id)

        def load() -> int:
            loads["count"] += 1
            return original_loader()

        return load

    monkeypatch.setattr(store, "_credit_shard_count_loader", counted_factory)

    assert _typed_authorize(store, key, estimate=70)[0] == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert _typed_authorize(store, key, estimate=70)[0] == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert loads["count"] == 1


def test_shard_count_refresh_reloads_after_min_interval() -> None:
    now = [100.0]
    cache = CreditShardCountCache(ttl_seconds=600, clock=lambda: now[0])
    loads = {"count": 0}

    def load(value: int) -> int:
        loads["count"] += 1
        return value

    assert cache.get("ws", lambda: load(2)) == 2
    assert cache.refresh("ws", lambda: load(3)) == 2
    assert loads["count"] == 1

    now[0] += REFRESH_MIN_INTERVAL_SECONDS + 0.1

    assert cache.refresh("ws", lambda: load(3)) == 3
    assert loads["count"] == 2


def test_refresh_failure_keeps_402(monkeypatch: pytest.MonkeyPatch) -> None:
    store, _database, key = _seed([100, 100], usage=[70, 70])

    def fail_refresh(_workspace_id: str) -> tuple[int, ...]:
        raise RuntimeError("transient refresh failure")

    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", fail_refresh)

    outcome, authorization = _typed_authorize(store, key, estimate=70)

    assert outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert authorization is None


def test_unsharded_rejection_skips_precheck_and_rebalance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

    store, _database, key = _seed([100], usage=[60])
    calls = _install_rebalance_spy(monkeypatch)

    def fail_precheck(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("unsharded rejection must not precheck")

    monkeypatch.setattr(rebalance_mod, "credit_headroom_precheck", fail_precheck)

    outcome, authorization = _typed_authorize(store, key, estimate=50)

    assert outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert authorization is None
    assert calls["count"] == 0


def test_unshard_behind_refresh_dedupe_returns_402_not_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a remote unshard whose count is hidden by the refresh
    dedupe window must NOT convert an underfunded reject into a RuntimeError
    500. The INCOMPLETE precheck verdict forces one dedupe-bypassing reload,
    re-runs authorize on the true shard set, and yields a clean 402."""
    calls = _install_rebalance_spy(monkeypatch)
    store, database, key = _seed([40, 40, 40, 40])

    # Freshly-loaded cache entry (inside the dedupe window) says 4 shards.
    assert store._credit_shard_counts.get(WORKSPACE_ID, lambda: 4) == 4
    # Remote unshard: rows consolidated to shard 0 only, account now count=1.
    _set_credit_rows(database, [40])
    store._write_entity(
        "credit",
        WORKSPACE_ID,
        CreditAccount(workspace_id=WORKSPACE_ID, shard_count=1),
    )

    outcome, authorization = _typed_authorize(store, key, estimate=60)

    assert outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert authorization is None
    assert calls["count"] == 0  # never entered the RW repair, never raised


def test_steal_race_retry_not_blocked_by_own_cooldown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the cooldown gates only a request's FIRST repair. When a
    concurrent request drains the consolidated target between rebalance commit
    and re-authorize (the steal race the retry loop exists for), the SAME
    request's second repair attempt must not be blocked by its own timestamp."""
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

    monkeypatch.setattr(rebalance_mod, "REBALANCE_COOLDOWN_SECONDS", 300.0)

    store, database, key = _seed([100, 100], usage=[30, 30])
    calls = {"count": 0}
    original = rebalance_mod.rebalance_credit_for_estimate

    def stealing_spy(*args: Any, **kwargs: Any) -> dict[str, int | str]:
        calls["count"] += 1
        result = original(*args, **kwargs)
        if calls["count"] == 1 and result["outcome"] == RebalanceOutcome.MOVED:
            # Simulate a concurrent request committing a 5-micro hold on the
            # freshly consolidated target before our re-authorize runs.
            target = int(result["target_shard"])
            row = database.typed[CREDIT_BALANCE_TABLE][(WORKSPACE_ID, target)]
            row["reserved"] = int(row["reserved"]) + 5
        return result

    monkeypatch.setattr(rebalance_mod, "rebalance_credit_for_estimate", stealing_spy)

    outcome, authorization = _typed_authorize(store, key, estimate=80)

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    assert calls["count"] == 2  # first repair stolen, second repair allowed
