from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from trusted_router.storage_gcp_authorize import AuthorizeOutcome
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
from trusted_router.storage_gcp_credit_rebalance import (
    RebalanceOutcome,
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
    assert nonpositive == RebalanceOutcome.NOT_NEEDED
    assert _typed_rows(database) == before


def test_rebalance_cooldown_suppresses_immediate_followers(
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

    second_outcome, second_authorization = _typed_authorize(
        store,
        key,
        estimate=60,
        idempotency_key="second",
    )
    assert second_outcome == AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert second_authorization is None
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

    monkeypatch.setattr(rebalance_mod, "rebalance_precheck", fail_precheck)

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
