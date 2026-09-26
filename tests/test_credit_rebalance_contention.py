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
    rebalance_credit_for_estimate,
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


@pytest.mark.xfail(
    strict=True, raises=StoreUnavailable,
    reason="convoy needs shard count to follow balance; see docs",
)
def test_small_balance_convoy_residual(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from trusted_router import storage_gcp as gcp
    from trusted_router import storage_gcp_authorize as authorize_mod

    store, database, key = _seed([750_000] * 16)
    candidates = tuple(range(16))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    # Every request is inside the real, unchanged cooldown window.
    monkeypatch.setattr(gcp.time, "monotonic", lambda: 100.0)
    waits: list[float] = []
    monkeypatch.setattr(gcp.time, "sleep", waits.append)
    attempts: list[tuple[int, ...]] = []
    original = authorize_mod.authorize_atomic

    def track(*args: Any, **kwargs: Any) -> dict[str, Any]:
        attempts.append(tuple(kwargs["credit_shard_candidates"]))
        return original(*args, **kwargs)

    monkeypatch.setattr(authorize_mod, "authorize_atomic", track)
    calls = _install_rebalance_spy(monkeypatch)
    caplog.set_level("INFO", logger="trusted_router.storage_gcp")
    cooldown_attempts: list[bool] = []
    original_cooldown = store._credit_rebalance_cooldown_allows

    def track_cooldown(workspace_id: str) -> bool:
        allowed = original_cooldown(workspace_id)
        cooldown_attempts.append(allowed)
        return allowed

    monkeypatch.setattr(store, "_credit_rebalance_cooldown_allows", track_cooldown)
    for request in range(12):
        attempts.clear()
        try:
            outcome, authorization = _typed_authorize(store, key, estimate=1_000_000)
        except StoreUnavailable as exc:
            assert str(exc) == "credit escrow rebalance is busy; retry"
            assert request == 1
            assert calls["count"] == 1
            assert waits == [0.25, 0.25]
            assert cooldown_attempts == [True, False, False, False]
            assert attempts == [candidates[:4]]
            assert "cooldown exhausted workspace=ws-contention attempts=3" in caplog.text
            raise
        assert outcome == AuthorizeOutcome.ACCEPTED
        assert authorization is not None
        assert calls["count"] == 1
        assert waits == []
        if request:
            # Target 0 is outside every follower's bounded random prefix.
            # The real all-shard precheck must discover and retry that exact row.
            assert 0 not in attempts[0]
            assert attempts[-1] == (0,)
        candidates = tuple(range(1, 16)) + (0,)

    rows = _typed_rows(database)
    assert rows[(WORKSPACE_ID, 0)]["reserved"] == 12_000_000
    assert sum(row["total_credits"] for row in rows.values()) == 12_000_000
    assert _typed_authorize(store, key, estimate=1_000_000)[0] == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS
    )
    assert calls["count"] == 1
    assert waits == []


@pytest.mark.parametrize("extra", [0, 1])
def test_needed_only_uses_largest_donor_at_any_balance(extra: int) -> None:
    # Needed-only transfers preserve sharding on both sides of the withdrawn limit.
    totals = [20_000_000, 40_000_000, 40_000_000 + extra]
    store, database, _key = _seed(totals)
    result = rebalance_credit_for_estimate(
        database, store._param_types, workspace_id=WORKSPACE_ID,
        shard_count=3, target_shard=0, estimate=50_000_000,
    )
    assert result == {
        "outcome": RebalanceOutcome.MOVED,
        "mode": "topped_up",
        "moved_micro": 30_000_000,
        "target_shard": 0,
    }
    expected = [50_000_000, 40_000_000, 10_000_000 + extra]
    assert [row["total_credits"] for row in _typed_rows(database).values()] == expected


def test_needed_only_preserves_money_and_ignores_negative_donor() -> None:
    # Headroom [5, 30, 25, -10]: debt requires only the 35-unit shortfall.
    store, database, _key = _seed(
        [20, 80, 65, 10], usage=[10, 40, 30, 20], reserved=[5, 10, 10, 0],
    )
    before = _typed_rows(database)
    result = rebalance_credit_for_estimate(
        database, store._param_types, workspace_id=WORKSPACE_ID,
        shard_count=4, target_shard=0, estimate=40,
    )
    after = _typed_rows(database)
    assert result == {
        "outcome": RebalanceOutcome.MOVED, "mode": "topped_up",
        "moved_micro": 35, "target_shard": 0,
    }
    assert sum(row["total_credits"] for row in after.values()) == sum(
        row["total_credits"] for row in before.values()
    )
    for shard, expected_total in enumerate([55, 50, 60, 10]):
        row = after[(WORKSPACE_ID, shard)]
        assert row["total_credits"] == expected_total
        assert row["total_usage"] == before[(WORKSPACE_ID, shard)]["total_usage"]
        assert row["reserved"] == before[(WORKSPACE_ID, shard)]["reserved"]
    assert after[(WORKSPACE_ID, 3)] == before[(WORKSPACE_ID, 3)]


def test_needed_only_repeated_authorization_rejects_debt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, key = _seed([0, 90, 90, 90, 90], usage=[200, 0, 0, 0, 0])
    candidates = (1, 0, 2, 3, 4)
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    calls = _install_rebalance_spy(monkeypatch)
    for expected, net in [
        (AuthorizeOutcome.ACCEPTED, 60),
        (AuthorizeOutcome.INSUFFICIENT_CREDITS, 60),
        (AuthorizeOutcome.INSUFFICIENT_CREDITS, 60),
    ]:
        outcome, _authorization = _typed_authorize(store, key, estimate=100)
        assert outcome == expected
        rows = _typed_rows(database)
        headroom = {
            shard: row["total_credits"] - row["total_usage"] - row["reserved"]
            for (_ws, shard), row in rows.items()
        }
        assert headroom[1] <= sum(headroom.values()) == net
        assert sum(row["total_credits"] for row in rows.values()) == 360
    assert calls["count"] == 1


def test_large_estimate_preserves_shards_for_small_requests(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    store, database, key = _seed([100_000_000] * 16)
    candidates = tuple(range(16))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    calls = _install_rebalance_spy(monkeypatch)
    caplog.set_level("INFO", logger="trusted_router.storage_gcp")
    assert _typed_authorize(store, key, estimate=101_000_000)[0] == AuthorizeOutcome.ACCEPTED
    rows = _typed_rows(database)
    assert sum(row["total_credits"] > row["reserved"] for row in rows.values()) == 15
    assert rows[(WORKSPACE_ID, 15)]["total_credits"] == 99_000_000
    for shard in range(1, 16):
        candidates = (shard,) + tuple(i for i in range(16) if i != shard)
        assert _typed_authorize(store, key, estimate=1_000_000)[0] == AuthorizeOutcome.ACCEPTED
        assert _typed_rows(database)[(WORKSPACE_ID, shard)]["reserved"] == 1_000_000
    assert calls["count"] == 1
    assert "outcome=moved moved_micro=1000000 mode=topped_up" in caplog.text


def test_second_process_reuses_funded_target_without_moving() -> None:
    store, database, _key = _seed([50] * 16)
    first = rebalance_credit_for_estimate(
        database, store._param_types, workspace_id=WORKSPACE_ID,
        shard_count=16, target_shard=7, estimate=100,
    )
    assert first["mode"] == "topped_up"
    before = _typed_rows(database)
    second = rebalance_credit_for_estimate(
        database, store._param_types, workspace_id=WORKSPACE_ID,
        shard_count=16, target_shard=7, estimate=100,
    )
    assert second == {
        "outcome": RebalanceOutcome.NOT_NEEDED, "moved_micro": 0, "target_shard": 7,
    }
    assert _typed_rows(database) == before


def test_authorize_repairs_own_target_after_peer_funds_another_shard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_gcp_authorize as authorize_mod
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

    store, database, key = _seed([50] * 16)
    candidates = tuple(range(16))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    original_precheck = rebalance_mod.credit_headroom_precheck
    original_authorize = authorize_mod.authorize_atomic
    attempts: list[tuple[int, ...]] = []

    def peer_after_precheck(*args: Any, **kwargs: Any) -> Any:
        verdict = original_precheck(*args, **kwargs)
        assert verdict.outcome == RebalanceOutcome.MOVED
        result = rebalance_credit_for_estimate(
            database, store._param_types, workspace_id=WORKSPACE_ID,
            shard_count=16, target_shard=7, estimate=100,
        )
        assert result["mode"] == "topped_up"
        return verdict

    def track(*args: Any, **kwargs: Any) -> dict[str, Any]:
        attempts.append(tuple(kwargs["credit_shard_candidates"]))
        return original_authorize(*args, **kwargs)

    monkeypatch.setattr(rebalance_mod, "credit_headroom_precheck", peer_after_precheck)
    monkeypatch.setattr(authorize_mod, "authorize_atomic", track)
    assert _typed_authorize(store, key, estimate=100)[0] == AuthorizeOutcome.ACCEPTED
    assert attempts == [candidates[:4], candidates[:4]]
    rows = _typed_rows(database)
    assert rows[(WORKSPACE_ID, 7)]["reserved"] == 0
    assert rows[(WORKSPACE_ID, 7)]["total_credits"] == 50
    assert rows[(WORKSPACE_ID, 0)]["total_credits"] == 100
    assert rows[(WORKSPACE_ID, 0)]["reserved"] == 100


def test_fragmented_exhaustion_verdict_and_gateway_402_skip_rebalance(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    from fastapi import HTTPException
    from starlette.requests import Request

    from trusted_router.config import Settings
    from trusted_router.routes.internal import gateway
    from trusted_router.schemas import GatewayAuthorizeRequest
    from trusted_router.storage import configure_store
    from trusted_router.storage_models import Workspace

    store, database, key = _seed([1] * 16)
    candidates = tuple(range(16))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    calls = _install_rebalance_spy(monkeypatch)
    caplog.set_level("INFO", logger="trusted_router.storage_gcp")
    assert _typed_authorize(store, key, estimate=16)[0] == AuthorizeOutcome.ACCEPTED
    assert "outcome=moved moved_micro=15 mode=topped_up" in caplog.text
    assert _typed_rows(database)[(WORKSPACE_ID, 0)]["total_credits"] == 16
    assert calls["count"] == 1
    before = _typed_rows(database)
    result = rebalance_credit_for_estimate(
        database, store._param_types, workspace_id=WORKSPACE_ID,
        shard_count=16, target_shard=0, estimate=17,
    )
    assert result["outcome"] == RebalanceOutcome.INSUFFICIENT
    assert result["moved_micro"] == 0
    assert _typed_rows(database) == before
    assert _typed_authorize(store, key, estimate=17)[0] == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS
    )
    store._write_entity(
        "workspace", WORKSPACE_ID,
        Workspace(id=WORKSPACE_ID, name="Exhausted", owner_user_id="user-exhausted"),
    )
    configure_store(store)
    request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, model="anthropic/claude-haiku-4.5",
        estimated_input_tokens=100, max_output_tokens=100,
    )
    with pytest.raises(HTTPException) as exc:
        gateway._authorize_gateway_sync(request, body, Settings(environment="test"))
    assert exc.value.status_code == 402
    assert calls["count"] == 1
    assert _typed_rows(database) == before


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


@pytest.mark.parametrize("last_usage, expected", [(99, RebalanceOutcome.INSUFFICIENT),
                                                   (96, RebalanceOutcome.NOT_NEEDED)])
def test_zero_estimate_precheck_respects_signed_affordability(
    last_usage: int, expected: str,
) -> None:
    """A zero estimate must not blindly select an overdrawn prefix shard."""
    store, database, _key = _seed(
        [100, 100, 100, 100, 100, 100],
        usage=[101, 101, 101, 101, 100, last_usage],
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

    assert precheck.outcome == expected
    assert precheck.candidate_shard == (4 if expected == RebalanceOutcome.NOT_NEEDED else None)
    # Rebalance's nonpositive-estimate fast path deliberately does not read:
    # unlike precheck, even the negative aggregate returns NOT_NEEDED (P3).
    assert rebalance_credit_for_estimate(
        database, store._param_types, workspace_id=WORKSPACE_ID,
        shard_count=6, target_shard=0, estimate=0,
    ) == {"outcome": RebalanceOutcome.NOT_NEEDED, "moved_micro": 0, "target_shard": 0}
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
    concurrent request drains the repaired target between rebalance commit
    and re-authorize (the steal race the retry loop exists for), the SAME
    request's second repair attempt must not be blocked by its own timestamp."""
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

    monkeypatch.setattr(rebalance_mod, "REBALANCE_COOLDOWN_SECONDS", 300.0)

    store, database, key = _seed([100, 160], usage=[30, 30], reserved=[0, 60])
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: (0, 1))
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: (0, 1))
    calls = {"count": 0}
    original = rebalance_mod.rebalance_credit_for_estimate

    def stealing_spy(*args: Any, **kwargs: Any) -> dict[str, int | str]:
        calls["count"] += 1
        result = original(*args, **kwargs)
        if calls["count"] == 1 and result["outcome"] == RebalanceOutcome.MOVED:
            # A peer holds 65 on the topped-up target (leaving 15 < 80),
            # while a refund releases 10 on the other shard (now 70). Funds are
            # sufficient but fragmented before our re-authorize runs.
            target = int(result["target_shard"])
            row = database.typed[CREDIT_BALANCE_TABLE][(WORKSPACE_ID, target)]
            row["reserved"] = int(row["reserved"]) + 65
            database.typed[CREDIT_BALANCE_TABLE][(WORKSPACE_ID, 1)]["reserved"] -= 10
        return result

    monkeypatch.setattr(rebalance_mod, "rebalance_credit_for_estimate", stealing_spy)

    outcome, authorization = _typed_authorize(store, key, estimate=80)

    assert outcome == AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    assert calls["count"] == 2  # first repair stolen, second repair allowed


def _headrooms(database: Any) -> list[int]:
    return [row["total_credits"] - row["total_usage"] - row["reserved"]
            for row in _typed_rows(database).values()]


def test_later_grant_and_donor_overage_cannot_expose_consolidated_debt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.storage_gcp_authorize import settle_atomic
    from trusted_router.storage_models import CreditProvenance

    store, database, key = _seed([500_000] * 4)
    candidates = (0, 1, 2, 3)
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    assert _typed_authorize(store, key, estimate=600_000)[0] == AuthorizeOutcome.ACCEPTED
    assert store.credit_workspace_typed_direct(
        WORKSPACE_ID, 2_000_000, "evt-later-grant",
        provenance=CreditProvenance.system_grant(),
    ) is True
    candidates = (1, 0, 2, 3)
    outcome, held = _typed_authorize(store, key, estimate=500_000)
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert settle_atomic(
        database, store._param_types, reservation_id=held.credit_reservation_id,
        actual_micro=2_000_000, settled_usage_type="Credits", success=True,
    )["outcome"] == "settled"
    assert sum(_headrooms(database)) == 1_400_000
    candidates = (0, 1, 2, 3)
    before = _typed_rows(database)
    assert _typed_authorize(store, key, estimate=1_500_000) == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS, None,
    )
    assert _typed_rows(database) == before
    assert _headrooms(database) == [500_000, -1_000_000, 1_000_000, 900_000]
    assert sum(row["total_credits"] for row in before.values()) == 4_000_000


def test_guarded_debit_rejects_funded_donor_when_signed_headroom_is_insufficient() -> None:
    store, database, _key = _seed([0, 100, 0], usage=[0, 0, 60])
    before = _typed_rows(database)
    assert store.debit_workspace_guarded(
        WORKSPACE_ID, 80, "evt-debt", kind="verification_fee",
    ) == "insufficient"
    assert _typed_rows(database) == before
    assert ("stripe_event", "evt-debt") not in database.rows
    assert store.list_credit_movements(WORKSPACE_ID) == []


@pytest.mark.parametrize("candidates", [(0, 1, 2), (1, 0, 2)])
def test_signed_affordability_precedes_any_funded_destination(
    candidates: tuple[int, ...],
) -> None:
    store, database, _key = _seed([0, 100, 0], usage=[0, 0, 60])
    before = _typed_rows(database)
    precheck = credit_headroom_precheck(
        database, store._param_types, workspace_id=WORKSPACE_ID,
        shard_count=3, shard_candidates=candidates, estimate=80,
    )
    assert precheck.outcome == RebalanceOutcome.INSUFFICIENT
    assert precheck.candidate_shard is None
    result = rebalance_credit_for_estimate(
        database, store._param_types, workspace_id=WORKSPACE_ID,
        shard_count=3, target_shard=candidates[0], estimate=80,
    )
    assert result["outcome"] == RebalanceOutcome.INSUFFICIENT
    assert result["moved_micro"] == 0
    assert _typed_rows(database) == before


def test_authorize_precheck_rejects_funded_donor_with_insufficient_signed_headroom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pad Astra's [0, 100, -60] with empty shards so the funded donor is outside
    # the bounded initial scan and authorization reaches the routing precheck.
    store, database, key = _seed([0, 0, 0, 0, 100, 0], usage=[0, 0, 0, 0, 0, 60])
    candidates = tuple(range(6))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    calls = _install_rebalance_spy(monkeypatch)
    before = _typed_rows(database)
    assert _typed_authorize(store, key, estimate=80) == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS, None,
    )
    assert _typed_rows(database) == before
    assert database.reservations == {}
    assert calls["count"] == 0


def test_debt_sequence_matches_needed_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, key = _seed([0, 90, 90, 90, 90, 90, 90], usage=[200, 0, 0, 0, 0, 0, 0])
    candidates = (1, 0, 2, 3, 4, 5, 6)
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    assert _typed_authorize(store, key, estimate=100)[0] == AuthorizeOutcome.ACCEPTED
    assert sum(_headrooms(database)) == 240
    candidates = (2, 0, 1, 3, 4, 5, 6)
    assert _typed_authorize(store, key, estimate=90)[0] == AuthorizeOutcome.ACCEPTED
    assert sum(_headrooms(database)) == 150
    candidates = (1, 0, 2, 3, 4, 5, 6)
    before = _typed_rows(database)
    assert _typed_authorize(store, key, estimate=200) == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS, None,
    )
    assert _typed_rows(database) == before
    assert sum(_headrooms(database)) == 150
    assert sum(row["total_credits"] for row in before.values()) == 540

    # Continue with one affordable hold. The next cold-path snapshot must count
    # the original debt before offering a still-funded donor (origin/main did not).
    candidates = (3, 0, 1, 2, 4, 5, 6)
    assert _typed_authorize(store, key, estimate=90)[0] == AuthorizeOutcome.ACCEPTED
    assert sum(_headrooms(database)) == 60
    before = _typed_rows(database)
    verdict = credit_headroom_precheck(
        database, store._param_types, workspace_id=WORKSPACE_ID,
        shard_count=7, shard_candidates=candidates, estimate=90,
    )
    assert verdict.outcome == RebalanceOutcome.INSUFFICIENT
    assert verdict.candidate_shard is None
    assert _typed_rows(database) == before


def test_donor_settlement_sequence_matches_needed_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.storage_gcp_authorize import settle_atomic

    store, database, key = _seed([50, 100, 50, 50])
    candidates = (1, 0, 2, 3)
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    outcome, held = _typed_authorize(store, key, estimate=50)
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert _headrooms(database) == [50, 50, 50, 50]
    candidates = (0, 1, 2, 3)
    assert _typed_authorize(store, key, estimate=60)[0] == AuthorizeOutcome.ACCEPTED
    assert sum(_headrooms(database)) == 140
    assert settle_atomic(
        database, store._param_types, reservation_id=held.credit_reservation_id,
        actual_micro=150, settled_usage_type="Credits", success=True,
    )["outcome"] == "settled"
    assert sum(_headrooms(database)) == 40
    before = _typed_rows(database)
    assert _typed_authorize(store, key, estimate=60) == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS, None,
    )
    assert _typed_rows(database) == before
    assert sum(_headrooms(database)) == 40
    assert sum(row["total_credits"] for row in before.values()) == 250

    # A smaller estimate fits a donor but exceeds net headroom after settlement.
    # Pin the actual routing precheck as well as the needed-only control above.
    verdict = credit_headroom_precheck(
        database, store._param_types, workspace_id=WORKSPACE_ID,
        shard_count=4, shard_candidates=candidates, estimate=50,
    )
    assert verdict.outcome == RebalanceOutcome.INSUFFICIENT
    assert verdict.candidate_shard is None
    assert _typed_rows(database) == before


@pytest.mark.parametrize(
    "total, settle, expected_rebalances", [(100_000_016, False, 12), (101_000_000, True, 12)],
)
def test_needed_only_residual_convoy_with_expired_cooldown(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    total: int, settle: bool, expected_rebalances: int,
) -> None:
    from trusted_router import storage_gcp as gcp
    from trusted_router.storage_gcp_authorize import settle_atomic

    store, database, key = _seed([total // 16] * 16)
    candidates = tuple(range(16))
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    now = 100.0
    monkeypatch.setattr(gcp.time, "monotonic", lambda: now)
    calls = _install_rebalance_spy(monkeypatch)
    caplog.set_level("INFO", logger="trusted_router.storage_gcp")
    for _ in range(12):
        now += 1  # Allow the existing cooldown; no timing-policy override.
        outcome, authorization = _typed_authorize(store, key, estimate=7_000_000)
        assert outcome == AuthorizeOutcome.ACCEPTED
        if settle:
            assert settle_atomic(
                database, store._param_types,
                reservation_id=authorization.credit_reservation_id,
                actual_micro=50_000, settled_usage_type="Credits", success=True,
            )["outcome"] == "settled"
    assert calls["count"] == expected_rebalances
    assert caplog.text.count("mode=topped_up") == expected_rebalances
    assert "mode=consolidated" not in caplog.text
    assert sum(_headrooms(database)) == total - 12 * (50_000 if settle else 7_000_000)


def test_guarded_debit_repairs_settlement_debt_before_subsequent_spending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.storage_gcp_authorize import settle_atomic

    store, database, key = _seed([1_500_000] * 3)
    candidates = (0, 1, 2)
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    outcome, held = _typed_authorize(store, key, estimate=1_000_000)
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert settle_atomic(
        database, store._param_types, reservation_id=held.credit_reservation_id,
        actual_micro=3_500_000, settled_usage_type="Credits", success=True,
    )["outcome"] == "settled"
    assert _headrooms(database) == [-2_000_000, 1_500_000, 1_500_000]
    assert store.debit_workspace_guarded(
        WORKSPACE_ID, 1_000_000, "evt-debt-repair", kind="verification_fee",
    ) == "accepted"
    candidates = (2, 0, 1)
    before = _typed_rows(database)
    assert _typed_authorize(store, key, estimate=1_500_000) == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS, None,
    )
    assert _typed_rows(database) == before
    assert _headrooms(database) == [0, 0, 0]
    assert sum(row["total_credits"] for row in before.values()) == 3_500_000
    assert store.debit_workspace_guarded(
        WORKSPACE_ID, 1_000_000, "evt-debt-repair", kind="verification_fee",
    ) == "duplicate"
    assert _typed_rows(database) == before
    assert len(store.list_credit_movements(WORKSPACE_ID)) == 1


def test_guarded_debit_repairs_target_before_future_donor_overage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.storage_gcp_authorize import settle_atomic

    store, database, key = _seed([40, 150])
    candidates = (1, 0)
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    outcome, held = _typed_authorize(store, key, estimate=50)
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert _headrooms(database) == [40, 100]
    assert store.debit_workspace_guarded(
        WORKSPACE_ID, 60, "evt-future-overage", kind="verification_fee",
    ) == "accepted"
    assert settle_atomic(
        database, store._param_types, reservation_id=held.credit_reservation_id,
        actual_micro=150, settled_usage_type="Credits", success=True,
    )["outcome"] == "settled"
    candidates = (0, 1)
    before = _typed_rows(database)
    assert _typed_authorize(store, key, estimate=40) == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS, None,
    )
    assert _typed_rows(database) == before
    assert _headrooms(database) == [0, -20]
    assert sum(row["total_credits"] for row in before.values()) == 130
    assert len(store.list_credit_movements(WORKSPACE_ID)) == 1


def test_guarded_debit_peer_reservation_after_repair_does_not_false_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod

    store, database, key = _seed([10, 90, 90])
    candidates = (1, 0, 2)
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    original = rebalance_mod.rebalance_credit_for_estimate
    repairs = 0

    def peer_after_repair(*args: Any, **kwargs: Any) -> dict[str, int | str]:
        nonlocal repairs
        repairs += 1
        result = original(*args, **kwargs)
        assert _typed_authorize(store, key, estimate=80)[0] == AuthorizeOutcome.ACCEPTED
        assert sum(_headrooms(database)) == 110
        return result

    monkeypatch.setattr(rebalance_mod, "rebalance_credit_for_estimate", peer_after_repair)
    assert store.debit_workspace_guarded(
        WORKSPACE_ID, 80, "evt-peer-race", kind="verification_fee",
    ) == "accepted"
    assert repairs == 1
    assert _headrooms(database) == [0, 10, 20]
    before = _typed_rows(database)
    assert store.debit_workspace_guarded(
        WORKSPACE_ID, 80, "evt-peer-race", kind="verification_fee",
    ) == "duplicate"
    assert _typed_rows(database) == before
    assert repairs == 1
    assert len(store.list_credit_movements(WORKSPACE_ID)) == 1


def test_authorize_repairs_target_after_peer_settlement_before_future_overage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_gcp_credit_rebalance as rebalance_mod
    from trusted_router.storage_gcp_authorize import settle_atomic

    store, database, key = _seed([40, 150])
    candidates = (1, 0)
    monkeypatch.setattr(store, "_credit_shard_candidates", lambda _ws: candidates)
    monkeypatch.setattr(store, "_refresh_credit_shard_candidates", lambda _ws: candidates)
    outcome, peer = _typed_authorize(store, key, estimate=100)
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert _headrooms(database) == [40, 50]
    original = rebalance_mod.credit_headroom_precheck
    peer_settled = False

    def settle_after_precheck(*args: Any, **kwargs: Any) -> Any:
        nonlocal peer_settled
        result = original(*args, **kwargs)
        if not peer_settled:
            assert result.outcome == RebalanceOutcome.MOVED
            assert settle_atomic(
                database, store._param_types, reservation_id=peer.credit_reservation_id,
                actual_micro=0, settled_usage_type="Credits", success=True,
            )["outcome"] == "settled"
            peer_settled = True
        return result

    monkeypatch.setattr(rebalance_mod, "credit_headroom_precheck", settle_after_precheck)
    candidates = (0, 1)
    assert _typed_authorize(store, key, estimate=60)[0] == AuthorizeOutcome.ACCEPTED
    assert peer_settled
    candidates = (1, 0)
    outcome, donor = _typed_authorize(store, key, estimate=50)
    assert outcome == AuthorizeOutcome.ACCEPTED
    assert settle_atomic(
        database, store._param_types, reservation_id=donor.credit_reservation_id,
        actual_micro=150, settled_usage_type="Credits", success=True,
    )["outcome"] == "settled"
    candidates = (0, 1)
    before = _typed_rows(database)
    assert _typed_authorize(store, key, estimate=40) == (
        AuthorizeOutcome.INSUFFICIENT_CREDITS, None,
    )
    assert _typed_rows(database) == before
    assert _headrooms(database) == [0, -20]
    assert sum(row["total_credits"] for row in before.values()) == 190
