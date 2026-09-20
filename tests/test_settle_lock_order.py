"""Observe credit-before-key statement order and rollback at the fake Spanner boundary."""

from __future__ import annotations

import copy
import json
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes.spanner import _FakeSnapshot, _KeySet, _ParamTypes
from tests.fakes.spanner_order import (
    authorize_credit_before_key,
    credit_before_key,
    record_statements,
    transaction_statements,
)
from tests.test_credit_row_sharding_increment3 import _seed as _seed_fragmented_credit
from tests.test_spend_lease_authorize import _atomic_harness
from tests.test_stage_d_heartbeat import NOW, _seed, _seed_reaper_counters
from trusted_router import storage_gcp_authorize as billing
from trusted_router.storage_errors import StoreUnavailable
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_models import Workspace


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str]]:
    return record_statements(monkeypatch)


@pytest.mark.parametrize("skip_key_limit", [False, True], ids=["capped", "uncapped"])
def test_authorize_credit_before_key(calls: list[tuple[Any, str]], skip_key_limit: bool) -> None:
    # Always exercise capped ordering as well, so reverting the flip invalidates
    # the uncapped compatibility case too.
    for skip in (False, skip_key_limit):
        db, _ = _seed()
        _seed_reaper_counters(db, hold=0)
        db.typed[KEY_LIMIT_TABLE][("key", 0)]["limit_micro"] = 1_000
        calls.clear()
        result = _authorize(db, skip_key_limit=skip)
        assert result["outcome"] == billing.AuthorizeOutcome.ACCEPTED
        statements = transaction_statements(calls)
        if skip:
            assert not any("tr_key_limit" in sql for sql in statements)
            assert any(sql.startswith("update tr_credit_balance") for sql in statements)
        else:
            authorize_credit_before_key(statements)
        res = db.reservations[result["reservation_id"]]
        assert res["credit_reserved_micro"] == 100
        assert res["key_reserved_micro"] == (0 if skip else 100)


def _authorize(
    db: Any, *, skip_key_limit: bool = False, has_credit_candidate: bool = True,
    **kwargs: Any,
) -> dict[str, Any]:
    return billing.authorize_atomic(
        db,
        _ParamTypes,
        workspace_id="workspace",
        key_hash="key",
        estimate=100,
        has_credit_candidate=has_credit_candidate,
        reservation_usage_type="Credits" if has_credit_candidate else "BYOK",
        idempotency_scope=None,
        idempotency_fingerprint=None,
        expires_at=NOW + timedelta(seconds=300),
        build_auth_body=lambda aid, rid: "{}",
        skip_key_limit=skip_key_limit,
        **kwargs,
    )


@pytest.mark.parametrize("failure", ["credit", "key", "missing_key"])
def test_authorize_rejection_order_and_rollback(
    calls: list[tuple[Any, str]], failure: str
) -> None:
    db, _ = _seed()
    _seed_reaper_counters(db, hold=0)
    db.typed[KEY_LIMIT_TABLE][("key", 0)]["limit_micro"] = 1_000
    if failure == "credit":
        db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_credits"] = 0
    elif failure == "key":
        db.typed[KEY_LIMIT_TABLE][("key", 0)]["reserved"] = 1_000
    else:
        db.typed[KEY_LIMIT_TABLE].clear()
    before = copy.deepcopy((db.typed, db.reservations, db.gateway_authorizations))
    calls.clear()
    result = _authorize(db)
    assert (
        result["outcome"]
        == {
            "credit": billing.AuthorizeOutcome.INSUFFICIENT_CREDITS,
            "key": billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED,
            "missing_key": billing.AuthorizeOutcome.KEY_MISSING,
        }[failure]
    )
    statements = transaction_statements(calls)
    if failure == "credit":
        assert statements and all(sql.startswith("update tr_credit_balance") for sql in statements)
    else:
        authorize_credit_before_key(statements)
    assert (db.typed, db.reservations, db.gateway_authorizations) == before


@pytest.mark.parametrize("path", ["settle", "typed_finalize", "reaper"])
@pytest.mark.parametrize("failure", [None, "credit", "key"])
def test_release_credit_before_key_and_rollback(
    calls: list[tuple[Any, str]],
    path: str,
    failure: str | None,
) -> None:
    # Check both healthy and failing traces: rollback alone cannot prove order.
    for broken in (None, failure) if failure else (None,):
        db, auth = _seed(cohort=False)
        _seed_reaper_counters(db)
        if broken:
            table = CREDIT_BALANCE_TABLE if broken == "credit" else KEY_LIMIT_TABLE
            row_key = ("workspace", 0) if broken == "credit" else ("key", 0)
            db.typed[table][row_key]["reserved"] = 0
        before = copy.deepcopy((db.typed, db.reservations, db.gateway_authorizations))
        calls.clear()
        if path == "settle":
            result = billing.settle_atomic(
                db,
                _ParamTypes,
                reservation_id="reservation",
                actual_micro=70,
                settled_usage_type="Credits",
                success=True,
            )["outcome"]
        elif path == "typed_finalize":
            auth.settled = True
            result = billing.typed_finalize_atomic(
                db,
                _ParamTypes,
                reservation_id="reservation",
                authorization_id=auth.id,
                actual_micro=70,
                settled_usage_type="Credits",
                success=True,
                now=NOW,
                authorization=auth,
                auth_body_settled="{}",
                outbox_available=False,
            )["outcome"]
        else:
            result = billing._finalize_reaped_reservation_atomic(
                db,
                _ParamTypes,
                reservation_id="reservation",
                reap_now=NOW + timedelta(seconds=301),
                guard_outbox=False,
                snapshot_booking_enabled=False,
                operational_analytics_outbox=None,
            ).outcome
        statements = transaction_statements(calls)
        if broken:
            assert result == billing.SettleOutcome.ERROR
            assert (db.typed, db.reservations, db.gateway_authorizations) == before
            credit_before_key(statements, require_both=False)
        else:
            assert result == billing.SettleOutcome.SETTLED
            credit_before_key(statements, key_last=True)
            assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["reserved"] == 0
            assert db.typed[KEY_LIMIT_TABLE][("key", 0)]["reserved"] == 0
            amount = 0 if path == "reaper" else 70
            assert db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]["total_usage"] == amount
            assert db.typed[KEY_LIMIT_TABLE][("key", 0)]["usage"] == amount


@pytest.mark.parametrize("has_credit_candidate", [False, True], ids=["byok", "credits"])
@pytest.mark.parametrize("paused", [False, True], ids=["active", "paused"])
def test_armed_pause_precedes_capped_key_and_rolls_back(
    calls: list[tuple[Any, str]], has_credit_candidate: bool, paused: bool,
) -> None:
    db, _ = _seed()
    _seed_reaper_counters(db, hold=0)
    # A pause intentionally takes precedence over an exhausted key cap.
    db.typed[KEY_LIMIT_TABLE][("key", 0)]["limit_micro"] = 0 if paused else 1_000
    row = db.typed[CREDIT_BALANCE_TABLE][("workspace", 0)]
    row.update(billing_pause_causes=["abuse"] if paused else [], pause_epoch=int(paused))
    before = copy.deepcopy((db.typed, db.reservations, db.gateway_authorizations))
    calls.clear()
    result = _authorize(
        db, has_credit_candidate=has_credit_candidate,
        trust_settings=SimpleNamespace(spend_lease_trust_eligibility_enabled=True),
    )
    statements = transaction_statements(calls)
    pause = next(i for i, sql in enumerate(statements)
                 if sql.startswith("select billing_pause_causes, pause_epoch"))
    if paused:
        assert result["outcome"] == "billing_paused"
        assert not any("tr_key_limit" in sql for sql in statements)
        assert (db.typed, db.reservations, db.gateway_authorizations) == before
        if has_credit_candidate:
            assert statements[0].startswith("update tr_credit_balance")
    else:
        assert result["outcome"] == billing.AuthorizeOutcome.ACCEPTED
        assert pause < next(i for i, sql in enumerate(statements) if "tr_key_limit" in sql)
        authorize_credit_before_key(statements)


def _fragmented_authorize(store: Any, key: Any, **kwargs: Any) -> tuple[str, Any]:
    options = dict(
        workspace_id=key.workspace_id, key_hash=key.hash, estimate=10_000,
        has_credit_candidate=True, reservation_usage_type="Credits",
        model_id="model", provider="provider", requested_model_id=None,
        candidate_model_ids=["model"], region="us", endpoint_id="endpoint",
        candidate_endpoint_ids=["endpoint"], idempotency_key=None,
        idempotency_fingerprint=None, key_usage_shards=4,
    )
    options.update(kwargs)
    return store.authorize_gateway_typed(**options)


def _fragmented_store(credits: list[int], key_limit: int) -> tuple[Any, Any, Any]:
    store, db, key = _seed_fragmented_credit(credits)
    key.usage_shard_count = 4
    store._write_entity("api_key", key.hash, key)
    rows = db.typed[KEY_LIMIT_TABLE]
    for row_key in list(rows):
        if row_key[0] == key.hash and row_key[1] >= 4:
            del rows[row_key]
    for shard in range(1, 4):
        rows[(key.hash, shard)] = {**rows[(key.hash, 0)], "shard": shard}
    assert store.api_keys.update(key.hash, {"limit_microdollars": key_limit}) is not None
    return store, db, key


@pytest.mark.parametrize(
    ("credits", "key_limit", "outcome"),
    [
        ([6_000, 6_000], 20_000, billing.AuthorizeOutcome.ACCEPTED),
        ([6_000, 6_000], 40_000, billing.AuthorizeOutcome.ACCEPTED),
        ([12_000, 0], 20_000, billing.AuthorizeOutcome.ACCEPTED),
        ([6_000, 6_000], 8_000, billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED),
        ([4_000, 4_000], 20_000, billing.AuthorizeOutcome.INSUFFICIENT_CREDITS),
    ],
    ids=["both", "credit_only", "key_only", "key_exhausted", "credit_exhausted"],
)
def test_fragmented_credit_and_key_repair_conserves_totals(
    credits: list[int], key_limit: int, outcome: str,
) -> None:
    store, db, key = _fragmented_store(credits, key_limit)
    verdict, authorization = _fragmented_authorize(store, key)
    assert verdict == outcome
    assert (key.hash in store._lifetime_cap_exhausted_keys) == (
        outcome == billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED
    )
    hold = 10_000 if outcome == billing.AuthorizeOutcome.ACCEPTED else 0
    credit_rows = list(db.typed[CREDIT_BALANCE_TABLE].values())
    key_rows = list(db.typed[KEY_LIMIT_TABLE].values())
    assert sum(row["total_credits"] for row in credit_rows) == sum(credits)
    assert sum(row["total_usage"] for row in credit_rows) == 0
    assert sum(row["reserved"] for row in credit_rows) == hold
    assert sum(row["limit_micro"] for row in key_rows) == key_limit
    assert sum(row["usage"] + row["byok_usage"] for row in key_rows) == 0
    assert sum(row["reserved"] for row in key_rows) == hold
    assert len(db.reservations) == int(bool(hold))
    if hold:
        assert authorization is not None
        reservation = db.reservations[authorization.credit_reservation_id]
        assert reservation["credit_reserved_micro"] == reservation["key_reserved_micro"] == hold
    else:
        assert authorization is None


def test_key_repair_retries_on_the_credit_shard_that_held_funds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Funds sit outside the bounded credit prefix AND the key escrow is fragmented.

    The credit fix is "retry that exact funded shard", not a rebalance. The key
    repair that follows must retry on that same shard; retrying on the original
    bounded prefix misses the funds again and turned this affordable request
    into a retryable 503.
    """
    from trusted_router import storage_gcp

    shards = billing.MAX_CREDIT_SHARD_ATTEMPTS_PER_TRANSACTION + 2
    credits = [0] * (shards - 1) + [12_000]
    store, db, key = _fragmented_store(credits, 20_000)
    # Deterministic order with the funded shard beyond the per-transaction bound.
    monkeypatch.setattr(
        storage_gcp, "randomized_credit_shards", lambda count: tuple(range(count))
    )
    verdict, authorization = _fragmented_authorize(store, key)
    assert verdict == billing.AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    reservation = db.reservations[authorization.credit_reservation_id]
    assert reservation["credit_shard"] == shards - 1
    assert reservation["credit_reserved_micro"] == reservation["key_reserved_micro"] == 10_000
    credit_rows = list(db.typed[CREDIT_BALANCE_TABLE].values())
    assert sum(row["reserved"] for row in credit_rows) == 10_000
    assert sum(row["total_credits"] for row in credit_rows) == 12_000


@pytest.mark.parametrize("initial_shards", [1, 2], ids=["new_split", "second_repair"])
def test_credit_split_during_key_repair_is_recovered(
    monkeypatch: pytest.MonkeyPatch, initial_shards: int,
) -> None:
    from trusted_router import storage_gcp
    from trusted_router import storage_gcp_key_escrow as escrow
    from trusted_router.storage_gcp_credit_shard_admin import reshard_credit_account

    store, db, key = _fragmented_store([12_000 // initial_shards] * initial_shards, 20_000)
    monkeypatch.setattr(storage_gcp, "randomized_credit_shards", lambda count: tuple(range(count)))
    rebalance = escrow.rebalance_key_limit_headroom
    repairs = 0
    cooldown_checks = 0
    cooldown_allows = store._credit_rebalance_cooldown_allows

    def cooldown(workspace_id: str) -> bool:
        nonlocal cooldown_checks
        cooldown_checks += 1
        # Any second check would be this request's own cooldown timestamp.
        return cooldown_checks == 1 and cooldown_allows(workspace_id)

    def split_after_repair(*args: Any, **kwargs: Any) -> bool:
        nonlocal repairs
        repairs += 1
        repaired = rebalance(*args, **kwargs)
        assert repaired
        workspace = Workspace(
            id=key.workspace_id, name="split during repair", owner_user_id="owner",
            billing_paused=True,
        )
        store._write_entity("workspace", workspace.id, workspace)
        split = reshard_credit_account(store, workspace.id, initial_shards * 2, apply=True)
        assert split.applied and split.ready, split.reasons
        workspace.billing_paused = False
        store._write_entity("workspace", workspace.id, workspace)
        assert [row["total_credits"] for row in db.typed[CREDIT_BALANCE_TABLE].values()] == (
            [12_000 // (initial_shards * 2)] * (initial_shards * 2)
        )
        return repaired

    monkeypatch.setattr(store, "_credit_rebalance_cooldown_allows", cooldown)
    monkeypatch.setattr(escrow, "rebalance_key_limit_headroom", split_after_repair)
    verdict, authorization = _fragmented_authorize(store, key)
    assert verdict == billing.AuthorizeOutcome.ACCEPTED
    assert repairs == cooldown_checks == 1
    _assert_repaired_totals(db, authorization, credit_shard=0)


def _assert_repaired_totals(db: Any, authorization: Any, *, credit_shard: int) -> None:
    assert authorization is not None
    reservation = db.reservations[authorization.credit_reservation_id]
    assert reservation["credit_shard"] == credit_shard
    assert reservation["credit_reserved_micro"] == reservation["key_reserved_micro"] == 10_000
    credit_rows = list(db.typed[CREDIT_BALANCE_TABLE].values())
    key_rows = list(db.typed[KEY_LIMIT_TABLE].values())
    assert sum(row["total_credits"] for row in credit_rows) == 12_000
    assert sum(row["total_usage"] for row in credit_rows) == 0
    assert sum(row["reserved"] for row in credit_rows) == 10_000
    assert sum(row["limit_micro"] for row in key_rows) == 20_000
    assert sum(row["usage"] + row["byok_usage"] for row in key_rows) == 0
    assert sum(row["reserved"] for row in key_rows) == 10_000
    assert len(db.reservations) == 1


@pytest.mark.parametrize("remote_split", [True, False], ids=["remote_split", "true_402"])
def test_second_credit_recovery_bypasses_own_refresh_dedupe(
    monkeypatch: pytest.MonkeyPatch, remote_split: bool,
) -> None:
    from trusted_router import storage_gcp
    from trusted_router import storage_gcp_key_escrow as escrow
    from trusted_router.storage_gcp_credit_shards import (
        REFRESH_MIN_INTERVAL_SECONDS,
        CreditShardCountCache,
    )

    store, db, key = _fragmented_store([6_000, 6_000], 20_000)
    monkeypatch.setattr(storage_gcp, "randomized_credit_shards", lambda count: tuple(range(count)))
    now = 0.0
    store._credit_shard_counts = CreditShardCountCache(clock=lambda: now)
    assert store._credit_shard_count(key.workspace_id) == 2
    # Old enough to really reload on the first recovery; time then stays fixed
    # so that recovery's refresh would blind the second inside the 2s dedupe.
    now = REFRESH_MIN_INTERVAL_SECONDS + 1
    attempts = 0
    reloads: list[int] = []
    invalidations: list[int] = []
    sleeps: list[float] = []
    authorize = billing.authorize_atomic
    get_account = store.get_credit_account
    invalidate = store._credit_shard_counts.invalidate
    rebalance = escrow.rebalance_key_limit_headroom

    def attempt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        return authorize(*args, **kwargs)

    def load(workspace_id: str) -> Any:
        reloads.append(attempts)
        return get_account(workspace_id)

    def force_reload(workspace_id: str) -> None:
        invalidations.append(attempts)
        invalidate(workspace_id)

    def change_after_repair(*args: Any, **kwargs: Any) -> bool:
        assert rebalance(*args, **kwargs)
        assert reloads == [1] and not invalidations
        rows = db.typed[CREDIT_BALANCE_TABLE]
        if remote_split:
            # Simulate another process: mutate the database directly, never
            # call this store's admin helpers (which invalidate its cache).
            template = dict(rows[(key.workspace_id, 0)])
            rows.clear()
            for shard in range(4):
                rows[(key.workspace_id, shard)] = {
                    **template, "shard": shard, "total_credits": 3_000,
                }
            config = db.rows[("credit", key.workspace_id)]
            config.body = json.dumps({**json.loads(config.body), "shard_count": 4})
        else:
            # Same topology, genuinely only 9,000 left after a peer spends.
            rows[(key.workspace_id, 0)]["total_usage"] += 3_000
        assert store._credit_shard_count(key.workspace_id) == 2
        return True

    monkeypatch.setattr(billing, "authorize_atomic", attempt)
    monkeypatch.setattr(store, "get_credit_account", load)
    monkeypatch.setattr(store._credit_shard_counts, "invalidate", force_reload)
    monkeypatch.setattr(escrow, "rebalance_key_limit_headroom", change_after_repair)
    monkeypatch.setattr(storage_gcp.time, "sleep", sleeps.append)
    verdict, authorization = _fragmented_authorize(store, key)
    assert reloads == [1, 3]  # One aged refresh and one forced JSON-row read.
    assert invalidations == [3]  # Never force on the first recovery too.
    # Unchanged budget: initial + key retry + 2*(count-change retry + 3 loop
    # retries) <= 10 authorize calls, and at most two 0.25s cooldown sleeps.
    # These concrete traces need only five / three calls and zero sleeps.
    assert attempts == (5 if remote_split else 3)
    assert not sleeps
    assert key.hash not in store._lifetime_cap_exhausted_keys
    if remote_split:
        assert verdict == billing.AuthorizeOutcome.ACCEPTED
        _assert_repaired_totals(db, authorization, credit_shard=0)
    else:
        assert verdict == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
        assert authorization is None and not db.reservations
        rows = db.typed[CREDIT_BALANCE_TABLE]
        assert sum(row["total_credits"] for row in rows.values()) == 12_000
        assert sum(row["total_usage"] for row in rows.values()) == 3_000
        assert all(row["reserved"] == 0 for row in rows.values())
        assert sum(row["limit_micro"] for row in db.typed[KEY_LIMIT_TABLE].values()) == 20_000
        assert all(row["reserved"] == 0 for row in db.typed[KEY_LIMIT_TABLE].values())


def test_credit_moved_beyond_prefix_during_key_repair_is_recovered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_gcp
    from trusted_router import storage_gcp_key_escrow as escrow
    from trusted_router.storage_gcp_credit_rebalance import (
        RebalanceOutcome,
        rebalance_credit_for_estimate,
    )

    shards = billing.MAX_CREDIT_SHARD_ATTEMPTS_PER_TRANSACTION + 2
    store, db, key = _fragmented_store([12_000] + [0] * (shards - 1), 20_000)
    monkeypatch.setattr(storage_gcp, "randomized_credit_shards", lambda count: tuple(range(count)))
    rebalance = escrow.rebalance_key_limit_headroom
    repairs = 0

    def move_after_repair(*args: Any, **kwargs: Any) -> bool:
        nonlocal repairs
        repairs += 1
        repaired = rebalance(*args, **kwargs)
        assert repaired
        moved = rebalance_credit_for_estimate(
            db, store._param_types, workspace_id=key.workspace_id,
            shard_count=shards, target_shard=shards - 1, estimate=12_000,
        )
        assert moved["outcome"] == RebalanceOutcome.MOVED
        assert moved["moved_micro"] == 12_000
        return repaired

    monkeypatch.setattr(escrow, "rebalance_key_limit_headroom", move_after_repair)
    verdict, authorization = _fragmented_authorize(store, key)
    assert verdict == billing.AuthorizeOutcome.ACCEPTED
    assert repairs == 1
    _assert_repaired_totals(db, authorization, credit_shard=shards - 1)


def test_credit_recovery_authorize_attempts_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, key = _fragmented_store([12_000, 0], 20_000)
    attempts = 0

    def insufficient(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        assert attempts <= 4
        return {"outcome": billing.AuthorizeOutcome.INSUFFICIENT_CREDITS}

    monkeypatch.setattr(billing, "authorize_atomic", insufficient)
    with pytest.raises(StoreUnavailable, match="credit headroom changed concurrently; retry"):
        _fragmented_authorize(store, key)
    # One initial run_authorize + three funded-shard retries. The count refresh
    # is unchanged, and no KEY_LIMIT_EXCEEDED means no key repair/second entry.
    assert attempts == 4
    assert key.hash not in store._lifetime_cap_exhausted_keys
    assert not db.reservations


def test_second_credit_recovery_key_rejection_does_not_repeat_key_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import storage_gcp_key_escrow as escrow

    store, db, key = _fragmented_store([12_000, 0], 20_000)
    outcomes = iter([
        billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED,
        billing.AuthorizeOutcome.INSUFFICIENT_CREDITS,
        billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED,
    ])
    attempts = 0
    repairs = 0
    rebalance = escrow.rebalance_key_limit_headroom

    def attempt(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        return {"outcome": next(outcomes)}

    def repair(*args: Any, **kwargs: Any) -> bool:
        nonlocal repairs
        repairs += 1
        return rebalance(*args, **kwargs)

    monkeypatch.setattr(billing, "authorize_atomic", attempt)
    monkeypatch.setattr(escrow, "rebalance_key_limit_headroom", repair)
    assert _fragmented_authorize(store, key) == (billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED, None)
    assert attempts == 3 and repairs == 1
    assert key.hash in store._lifetime_cap_exhausted_keys
    assert not db.reservations


@pytest.mark.parametrize("credits", [[6_000, 6_000], [12_000, 0]], ids=["both", "key_only"])
def test_credit_consumed_after_key_repair_is_retryable(
    monkeypatch: pytest.MonkeyPatch, credits: list[int],
) -> None:
    from trusted_router import storage_gcp_key_escrow as escrow

    store, db, key = _fragmented_store(credits, 20_000)
    rebalance = escrow.rebalance_key_limit_headroom

    def consume_after_repair(*args: Any, **kwargs: Any) -> bool:
        repaired = rebalance(*args, **kwargs)
        assert repaired
        # Another request commits spend between the key repair and our retry.
        row = max(db.typed[CREDIT_BALANCE_TABLE].values(), key=lambda r: r["total_credits"])
        row["total_usage"] += 3_000
        return repaired

    monkeypatch.setattr(escrow, "rebalance_key_limit_headroom", consume_after_repair)
    # The second recovery now proves aggregate exhaustion (9,000 < 10,000),
    # so an honest 402 is preferable to the previous unproven retryable 503.
    verdict, authorization = _fragmented_authorize(store, key)
    assert verdict == billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
    assert authorization is None
    assert sum(row["total_credits"] - row["total_usage"]
               for row in db.typed[CREDIT_BALANCE_TABLE].values()) == 9_000
    assert sum(row["reserved"] for row in db.typed[CREDIT_BALANCE_TABLE].values()) == 0
    assert sum(row["reserved"] for row in db.typed[KEY_LIMIT_TABLE].values()) == 0
    assert not db.reservations


LIFETIME_SQL = "SELECT shard, limit_micro, usage, byok_usage, reserved, include_byok"


def _lifetime_snapshot_reads(db: Any) -> list[str]:
    return [sql for sql in db.snapshot_sql if LIFETIME_SQL in sql]


def test_lifetime_cap_exact_headroom_is_accepted(calls: list[tuple[Any, str]]) -> None:
    store, db, key = _fragmented_store([12_000], 10_000)
    calls.clear()
    db.snapshot_sql.clear()
    verdict, authorization = _fragmented_authorize(store, key)
    assert verdict == billing.AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    assert any(sql.startswith("update tr_credit_balance") for _, sql in calls)
    assert sum(row["reserved"] for row in db.typed[KEY_LIMIT_TABLE].values()) == 10_000
    assert sum(row["limit_micro"] for row in db.typed[KEY_LIMIT_TABLE].values()) == 10_000
    assert not _lifetime_snapshot_reads(db)
    # Fragmented escrow was repaired; its provisional rejection must not cache.
    assert key.hash not in store._lifetime_cap_exhausted_keys


@pytest.mark.parametrize("restore", ["raise_cap", "release_reserved"])
def test_lifetime_cap_headroom_drops_cache_and_snapshot_cost(
    restore: str, calls: list[tuple[Any, str]], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, key = _fragmented_store([30_000], 8_000 if restore == "raise_cap" else 40_000)
    if restore == "release_reserved":
        for row in db.typed[KEY_LIMIT_TABLE].values():
            row["reserved"] = 9_000
    options = {"idempotency_key": "headroom", "idempotency_fingerprint": "same-body"}
    assert _fragmented_authorize(store, key, **options)[0] == billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED
    assert key.hash in store._lifetime_cap_exhausted_keys

    snapshots: list[tuple[Any, str]] = []
    execute_sql = _FakeSnapshot.execute_sql

    def record_snapshot(snapshot: Any, sql: str, **kwargs: Any) -> Any:
        snapshots.append((snapshot, sql))
        return execute_sql(snapshot, sql, **kwargs)

    monkeypatch.setattr(_FakeSnapshot, "execute_sql", record_snapshot)
    calls.clear()
    db.snapshot_sql.clear()
    assert _fragmented_authorize(store, key, **options)[0] == billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED
    assert not calls and not db.reservations
    assert len(db.snapshot_sql) == 2
    assert LIFETIME_SQL in db.snapshot_sql[0]
    assert "FROM tr_reservation WHERE idempotency_scope=@scope" in db.snapshot_sql[1]
    assert snapshots[0][0] is snapshots[1][0]  # ONE multi-use snapshot.
    assert snapshots[0][0]._multi_use

    if restore == "raise_cap":
        assert store.api_keys.update(key.hash, {"limit_microdollars": 40_000}) is not None
    else:
        for row in db.typed[KEY_LIMIT_TABLE].values():
            row["reserved"] = 0
    db.snapshot_sql.clear()
    verdict, authorization = _fragmented_authorize(store, key, **options)
    assert verdict == billing.AuthorizeOutcome.ACCEPTED and authorization is not None
    assert key.hash not in store._lifetime_cap_exhausted_keys
    assert len(_lifetime_snapshot_reads(db)) == 1
    assert LIFETIME_SQL in db.snapshot_sql[0]
    # Headroom needs no reservation lookup, even when an idempotency key exists.
    assert not any("FROM tr_reservation" in sql for sql in db.snapshot_sql)
    db.snapshot_sql.clear()
    assert _fragmented_authorize(store, key, idempotency_key="next")[0] == billing.AuthorizeOutcome.ACCEPTED
    assert not _lifetime_snapshot_reads(db)
    assert key.hash not in store._lifetime_cap_exhausted_keys


def test_lifetime_cap_exhausted_idempotent_authorize_replays(
    calls: list[tuple[Any, str]],
) -> None:
    store, db, key = _fragmented_store([30_000], 10_000)
    options = {"idempotency_key": "cap-retry", "idempotency_fingerprint": "same-body"}
    verdict, authorization = _fragmented_authorize(store, key, **options)
    assert verdict == billing.AuthorizeOutcome.ACCEPTED
    assert key.hash not in store._lifetime_cap_exhausted_keys
    assert _fragmented_authorize(store, key)[0] == billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED
    assert key.hash in store._lifetime_cap_exhausted_keys
    before = copy.deepcopy((db.typed, db.reservations))
    calls.clear()
    db.snapshot_sql.clear()
    verdict, replay = _fragmented_authorize(store, key, **options)
    assert verdict == billing.AuthorizeOutcome.REPLAY
    assert replay == authorization
    assert not any("tr_credit_balance" in sql for _, sql in calls)
    assert (db.typed, db.reservations) == before
    assert len(_lifetime_snapshot_reads(db)) == 1
    assert key.hash in store._lifetime_cap_exhausted_keys


def test_lifetime_cap_excluded_byok_is_accepted() -> None:
    store, db, key = _fragmented_store([12_000], 0)
    assert store.api_keys.update(key.hash, {"include_byok_in_limit": False}) is not None
    store._lifetime_cap_exhausted_keys.add(key.hash)
    db.snapshot_sql.clear()
    verdict, authorization = _fragmented_authorize(
        store, key, has_credit_candidate=False, reservation_usage_type="BYOK",
    )
    assert verdict == billing.AuthorizeOutcome.ACCEPTED
    assert authorization is not None
    reservation = db.reservations[authorization.credit_reservation_id]
    assert reservation["credit_reserved_micro"] == reservation["key_reserved_micro"] == 0
    assert all(row["reserved"] == 0 for row in db.typed[KEY_LIMIT_TABLE].values())
    assert len(_lifetime_snapshot_reads(db)) == 1
    assert key.hash not in store._lifetime_cap_exhausted_keys


def test_cached_lifetime_exhaustion_preserves_idempotency_mismatch(
    calls: list[tuple[Any, str]],
) -> None:
    store, db, key = _fragmented_store([30_000], 10_000)
    options = {"idempotency_key": "cap-conflict", "idempotency_fingerprint": "original"}
    assert _fragmented_authorize(store, key, **options)[0] == billing.AuthorizeOutcome.ACCEPTED
    assert _fragmented_authorize(store, key)[0] == billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED
    assert key.hash in store._lifetime_cap_exhausted_keys
    before = copy.deepcopy((db.typed, db.reservations, db.gateway_authorizations))
    options["idempotency_fingerprint"] = "different-body"
    for cached in (True, False):
        if not cached:
            store._lifetime_cap_exhausted_keys.discard(key.hash)
        calls.clear()
        db.snapshot_sql.clear()
        assert _fragmented_authorize(store, key, **options) == (
            billing.AuthorizeOutcome.IDEMPOTENCY_MISMATCH, None,
        )
        assert (db.typed, db.reservations, db.gateway_authorizations) == before
        assert (key.hash in store._lifetime_cap_exhausted_keys) == cached
        assert len(_lifetime_snapshot_reads(db)) == int(cached)
        assert calls  # Both classifications come from the transaction.
        assert not any("tr_credit_balance" in sql for _, sql in calls)


@pytest.mark.parametrize("estimate", [0, 10_000])
def test_lifetime_cap_negative_net_headroom_still_accepts_funded_shard(
    monkeypatch: pytest.MonkeyPatch, estimate: int,
) -> None:
    from trusted_router import storage_gcp

    store, db, key = _fragmented_store([100_000], 40_000)
    monkeypatch.setattr(storage_gcp, "randomized_credit_shards", lambda count: tuple(range(count)))
    verdict, authorization = _fragmented_authorize(store, key)
    assert verdict == billing.AuthorizeOutcome.ACCEPTED and authorization is not None
    # Actual usage above the hold creates the reviewer's vector through real
    # authorize/settle primitives, with no fabricated negative counters.
    assert billing.settle_atomic(
        db, store._param_types, reservation_id=authorization.credit_reservation_id,
        actual_micro=41_000, settled_usage_type="Credits", success=True,
    )["outcome"] == billing.SettleOutcome.SETTLED
    rows = db.typed[KEY_LIMIT_TABLE]
    assert [rows[(key.hash, shard)]["limit_micro"] - rows[(key.hash, shard)]["usage"]
            for shard in range(4)] == [-31_000, 10_000, 10_000, 10_000]
    # Learn exhaustion from a real rejection; the smaller follow-up fits shard 1.
    assert _fragmented_authorize(store, key, estimate=10_001) == (
        billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED, None,
    )
    assert key.hash in store._lifetime_cap_exhausted_keys
    db.snapshot_sql.clear()
    verdict, accepted = _fragmented_authorize(store, key, estimate=estimate)
    assert verdict == billing.AuthorizeOutcome.ACCEPTED and accepted is not None
    assert db.reservations[accepted.credit_reservation_id]["key_shard"] == 1
    assert key.hash not in store._lifetime_cap_exhausted_keys
    assert len(_lifetime_snapshot_reads(db)) == 1
    assert sum(row["limit_micro"] for row in rows.values()) == 40_000
    assert sum(row["usage"] for row in rows.values()) == 41_000
    assert sum(row["reserved"] for row in rows.values()) == estimate
    assert sum(row["total_usage"] for row in db.typed[CREDIT_BALANCE_TABLE].values()) == 41_000
    assert sum(row["reserved"] for row in db.typed[CREDIT_BALANCE_TABLE].values()) == estimate


def test_lifetime_cap_pooled_headroom_is_accepted() -> None:
    store, db, key = _fragmented_store([30_000], 24_000)
    rows = db.typed[KEY_LIMIT_TABLE]
    del rows[(key.hash, 2)], rows[(key.hash, 3)]
    assert [row["limit_micro"] for row in rows.values()] == [6_000, 6_000]
    assert _fragmented_authorize(store, key, estimate=12_001, key_usage_shards=2) == (
        billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED, None,
    )
    assert key.hash in store._lifetime_cap_exhausted_keys
    assert billing.key_lifetime_cap_precheck(
        db, store._param_types, key_hash=key.hash, estimate=10_000,
        has_credit_candidate=True, shard_count=2,
    ) == billing.HEADROOM
    verdict, authorization = _fragmented_authorize(store, key, key_usage_shards=2)
    assert verdict == billing.AuthorizeOutcome.ACCEPTED and authorization is not None
    assert key.hash not in store._lifetime_cap_exhausted_keys
    assert sum(row["limit_micro"] for row in rows.values()) == 12_000
    assert sum(row["reserved"] for row in rows.values()) == 10_000


def test_lifetime_cap_all_negative_headroom_rejects_zero_estimate() -> None:
    store, db, key = _fragmented_store([30_000], 40_000)
    for row in db.typed[KEY_LIMIT_TABLE].values():
        row["usage"] = 10_001
    assert billing.key_lifetime_cap_precheck(
        db, store._param_types, key_hash=key.hash, estimate=0,
        has_credit_candidate=True, shard_count=4,
    ) == billing.EXHAUSTED
    assert key.hash not in store._lifetime_cap_exhausted_keys
    assert _fragmented_authorize(store, key, estimate=0) == (
        billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED, None,
    )
    assert key.hash in store._lifetime_cap_exhausted_keys
    assert not db.reservations


def test_lifetime_cap_exhausted_implies_uncached_transaction_rejects() -> None:
    rng = random.Random(20260919)  # noqa: S311 - reproducible test vectors, not secrets.
    exhausted = 0
    for _ in range(64):
        # Each vector gets a fresh, uncached store. All counter values are
        # nonnegative; signed headroom can arise from overshooting at settle.
        store, db, key = _fragmented_store([100_000], 80_000)
        include_byok = rng.choice([False, True])
        for row in db.typed[KEY_LIMIT_TABLE].values():
            consumed = 20_000 - rng.randint(-31_000, 12_000)
            reserved = rng.randint(0, consumed)
            byok = rng.randint(0, consumed - reserved)
            row.update(
                reserved=reserved, byok_usage=byok, include_byok=include_byok,
                usage=consumed - reserved - (byok if include_byok else 0),
            )
        estimate = rng.choice([0, 10_000, 20_000])
        verdict = billing.key_lifetime_cap_precheck(
            db, store._param_types, key_hash=key.hash, estimate=estimate,
            has_credit_candidate=True, shard_count=4,
        )
        if verdict == billing.EXHAUSTED:
            exhausted += 1
            assert key.hash not in store._lifetime_cap_exhausted_keys
            before = copy.deepcopy(db.typed)
            assert _fragmented_authorize(store, key, estimate=estimate) == (
                billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED, None,
            )
            assert db.typed == before and not db.reservations
    assert 20 <= exhausted < 64  # Exercise the implication, not a vacuous pass.


@pytest.mark.parametrize("key_limit", [8_000, 20_000], ids=["reject", "accept"])
def test_lifetime_cap_snapshot_failure_defers_to_transaction(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, str]],
    caplog: pytest.LogCaptureFixture, key_limit: int,
) -> None:
    store, db, key = _fragmented_store([12_000], key_limit)
    store._lifetime_cap_exhausted_keys.add(key.hash)
    execute_sql = _FakeSnapshot.execute_sql

    def fail_cap_snapshot(snapshot: Any, sql: str, **kwargs: Any) -> Any:
        if LIFETIME_SQL in sql:
            raise RuntimeError("snapshot unavailable")
        return execute_sql(snapshot, sql, **kwargs)

    monkeypatch.setattr(_FakeSnapshot, "execute_sql", fail_cap_snapshot)
    calls.clear()
    verdict, authorization = _fragmented_authorize(store, key)
    expected = billing.AuthorizeOutcome.ACCEPTED if key_limit == 20_000 else billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED
    assert verdict == expected
    assert (authorization is not None) == (key_limit == 20_000)
    assert any(sql.startswith("update tr_credit_balance") for _, sql in calls)
    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "key lifetime-cap snapshot failed" in warnings[0].message
    assert key.hash in store._lifetime_cap_exhausted_keys
    hold = 10_000 if authorization else 0
    assert sum(row["reserved"] for row in db.typed[CREDIT_BALANCE_TABLE].values()) == hold


def test_lifetime_cap_precheck_rejection_keeps_window_decision(
    monkeypatch: pytest.MonkeyPatch, calls: list[tuple[Any, str]],
) -> None:
    from trusted_router.spend_windows import KeyWindowLimitDecision

    store, db, key = _fragmented_store([12_000], 8_000)
    decision = KeyWindowLimitDecision(
        window="day", limit=50_000, remaining=40_000,
        resets_at=NOW + timedelta(hours=1), reset_seconds=3_600, allowed=True,
    )
    monkeypatch.setattr(billing, "key_window_limit_decision", lambda *args, **kwargs: decision)
    before = copy.deepcopy(db.typed)
    for cached in (False, True):
        calls.clear()
        db.snapshot_sql.clear()
        verdict, authorization = _fragmented_authorize(store, key, window_limits={"day": 50_000})
        assert verdict == billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED and authorization is None
        assert isinstance(verdict, billing.AuthorizeVerdict) and verdict.rate_limit is decision
        assert key.hash in store._lifetime_cap_exhausted_keys
        assert db.typed == before and not db.reservations
        if cached:
            assert not any(sql.startswith("update tr_credit_balance") for _, sql in calls)
            assert not calls  # No RW statements, including key-escrow repair.
            assert len(_lifetime_snapshot_reads(db)) == 1
        else:
            assert any(sql.startswith("update tr_credit_balance") for _, sql in calls)
            assert not _lifetime_snapshot_reads(db)


@pytest.mark.parametrize("failure", ["window", "credit", "unavailable"])
def test_only_lifetime_rejection_is_cached(failure: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from trusted_router.spend_windows import KeyWindowLimitDecision

    store, db, key = _fragmented_store([0 if failure == "credit" else 12_000], 20_000)
    options: dict[str, Any] = {}
    if failure == "window":
        decision = KeyWindowLimitDecision(
            window="day", limit=5_000, remaining=0,
            resets_at=NOW + timedelta(hours=1), reset_seconds=3_600, allowed=False,
        )
        monkeypatch.setattr(billing, "key_window_limit_decision", lambda *args, **kwargs: decision)
        options["window_limits"] = {"day": 5_000}
    elif failure == "unavailable":
        def unavailable(*args: Any, **kwargs: Any) -> Any:
            raise StoreUnavailable("unavailable")
        monkeypatch.setattr(billing, "authorize_atomic", unavailable)
    if failure == "unavailable":
        with pytest.raises(StoreUnavailable, match="unavailable"):
            _fragmented_authorize(store, key)
    else:
        expected = (
            f"{billing.AuthorizeOutcome.KEY_WINDOW_LIMIT_EXCEEDED}:day"
            if failure == "window" else billing.AuthorizeOutcome.INSUFFICIENT_CREDITS
        )
        assert _fragmented_authorize(store, key, **options) == (expected, None)
    assert not db.reservations
    assert key.hash not in store._lifetime_cap_exhausted_keys


@pytest.mark.parametrize("seeded", [False, True])
def test_uncapped_authorize_skips_lifetime_snapshot(
    monkeypatch: pytest.MonkeyPatch, seeded: bool,
) -> None:
    store, db, key = _fragmented_store([30_000], 20_000)
    assert store.api_keys.update(key.hash, {"limit_microdollars": None}) is not None
    if seeded:
        store._lifetime_cap_exhausted_keys.add(key.hash)

    def unexpected(*args: Any, **kwargs: Any) -> str:
        pytest.fail("known uncapped key paid for a lifetime-cap snapshot")

    monkeypatch.setattr(billing, "key_lifetime_cap_precheck", unexpected)
    db.snapshot_sql.clear()
    assert _fragmented_authorize(store, key, skip_key_limit=True)[0] == billing.AuthorizeOutcome.ACCEPTED
    assert not _lifetime_snapshot_reads(db)
    assert (key.hash in store._lifetime_cap_exhausted_keys) == seeded
    # Even an unexpected lifetime outcome must respect the caller's skip flag.
    monkeypatch.setattr(billing, "authorize_atomic", lambda *args, **kwargs: {
        "outcome": billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED,
    })
    assert _fragmented_authorize(store, key, skip_key_limit=True, key_usage_shards=1) == (
        billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED, None,
    )
    assert (key.hash in store._lifetime_cap_exhausted_keys) == seeded


@pytest.mark.parametrize("state", ["missing", "incomplete", "uncapped", "byok_excluded"])
def test_lifetime_cap_uncertain_rows_pass_through(state: str) -> None:
    store, db, key = _fragmented_store([12_000], 0)
    rows = db.typed[KEY_LIMIT_TABLE]
    if state == "missing":
        rows.clear()
    elif state == "incomplete":
        del rows[(key.hash, 3)]
    elif state == "uncapped":
        rows[(key.hash, 3)]["limit_micro"] = None
    else:
        rows[(key.hash, 3)]["include_byok"] = False
    assert billing.key_lifetime_cap_precheck(
        db, store._param_types, key_hash=key.hash, estimate=10_000,
        has_credit_candidate=state != "byok_excluded", shard_count=4,
    ) == billing.HEADROOM


@pytest.mark.parametrize("counter", ["usage", "byok_usage", "reserved"])
@pytest.mark.parametrize("include_byok", [False, True])
def test_lifetime_cap_headroom_matches_reserve_arithmetic(counter: str, include_byok: bool) -> None:
    store, db, key = _fragmented_store([12_000], 20_000)
    for row in db.typed[KEY_LIMIT_TABLE].values():
        row[counter] = 3_000
        row["include_byok"] = include_byok
    assert billing.key_lifetime_cap_precheck(
        db, store._param_types, key_hash=key.hash, estimate=10_000,
        has_credit_candidate=True, shard_count=4,
    ) == (billing.EXHAUSTED if counter != "byok_usage" or include_byok else billing.HEADROOM)


def test_exhausted_key_cache_lru_bound() -> None:
    cache = billing.ExhaustedKeyCache(max_entries=2)
    cache.add("oldest")
    cache.add("newer")
    assert cache.contains("oldest")  # Membership refreshes recency.
    cache.add("newest")
    assert not cache.contains("newer")
    assert len(cache) == 2
    cache.add("oldest")  # Adding an existing entry also refreshes it.
    cache.add("last")
    assert "newest" not in cache
    assert "oldest" in cache and "last" in cache
    cache.discard("absent")
    assert len(cache) == 2
    cache.discard("last")
    assert len(cache) == 1 and "last" not in cache
    assert "absent" not in cache
    with pytest.raises(ValueError, match="max_entries must be positive"):
        billing.ExhaustedKeyCache(max_entries=0)


def test_exhausted_key_cache_threaded_bound() -> None:
    cache = billing.ExhaustedKeyCache(max_entries=16)

    def hammer(worker: int) -> None:
        for i in range(300):
            key = f"{worker}-{i % 32}"
            cache.add(key)
            cache.contains(key)
            if i % 3 == 0:
                cache.discard(key)
            assert len(cache) <= 16

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(hammer, range(4)))  # Propagate every worker exception.
    assert len(cache) <= 16


@pytest.mark.parametrize("table", [CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE])
@pytest.mark.parametrize("method", ["insert_or_update", "delete"])
def test_order_spy_records_counter_mutations(
    calls: list[tuple[Any, str]], table: str, method: str,
) -> None:
    db, _ = _seed()
    _seed_reaper_counters(db)
    calls.clear()

    def mutate(transaction: Any) -> None:
        if method == "delete":
            transaction.delete(table, _KeySet(keys=[("workspace" if table == CREDIT_BALANCE_TABLE else "key", 0)]))
        else:
            row = next(iter(db.typed[table].values()))
            transaction.insert_or_update(table=table, columns=tuple(row), values=[tuple(row.values())])

    db.run_in_transaction(mutate)
    assert [sql for _, sql in calls] == [f"mutation:{method} {table}"]
    # Counter mutations must not pass the hot-path DML invariant even if the
    # SQL reads alone appear to be ordered correctly.
    with pytest.raises(AssertionError):
        credit_before_key([
            "select reserved from tr_credit_balance", "select reserved from tr_key_limit",
            *[sql for _, sql in calls],
        ])


@pytest.mark.parametrize("escrowed", [False, True], ids=["ordinary_hold", "lease_escrowed"])
@pytest.mark.parametrize("key_exhausted", [False, True], ids=["accepted", "key_rejected"])
def test_spend_lease_credit_before_key_and_rollback(
    calls: list[tuple[Any, str]], escrowed: bool, key_exhausted: bool,
) -> None:
    db, plan, ledger = _atomic_harness()
    if key_exhausted:
        db.typed[KEY_LIMIT_TABLE][("key-hash", 0)]["limit_micro"] = 0
    before = copy.deepcopy((
        db.typed, db.rows, db.reservations, db.gateway_authorizations,
        db.spend_lease_arbitrations, db.spend_lease_open,
    ))
    calls.clear()
    result = billing.authorize_atomic(
        db, _ParamTypes, workspace_id="workspace-1", key_hash="key-hash",
        estimate=500, has_credit_candidate=True, reservation_usage_type="Credits",
        idempotency_scope="scope-1", idempotency_fingerprint="fingerprint-1",
        expires_at=NOW + timedelta(hours=1), build_auth_body=lambda aid, rid: "{}",
        authorization_id=plan.provisional_id,
        spend_lease_hook=lambda tx, shard: plan.transaction_hook(
            tx, _ParamTypes, "workspace-1", shard,
        ),
        credit_escrowed_by_spend_lease=escrowed,
    )
    authorize_credit_before_key(transaction_statements(calls))
    assert ledger.binds == 0
    if key_exhausted:
        assert result["outcome"] == billing.AuthorizeOutcome.KEY_LIMIT_EXCEEDED
        assert (
            db.typed, db.rows, db.reservations, db.gateway_authorizations,
            db.spend_lease_arbitrations, db.spend_lease_open,
        ) == before
    else:
        assert result["outcome"] == billing.AuthorizeOutcome.ACCEPTED
        assert result["bound"] is True
        assert db.typed[CREDIT_BALANCE_TABLE][("workspace-1", 0)]["reserved"] == (
            plan.artifact.cap_micro + (0 if escrowed else 500)
        )
        assert db.typed[KEY_LIMIT_TABLE][("key-hash", 0)]["reserved"] == 500


@pytest.mark.parametrize("receipt", [False, True], ids=["ordinary_fallback", "admission_rejected"])
def test_spend_lease_inverse_credit_before_key_and_admission_rollback(
    calls: list[tuple[Any, str]], receipt: bool,
) -> None:
    from trusted_router.storage_gcp_spend_lease import register_claim

    db, plan, ledger = _atomic_harness()
    db.run_in_transaction(lambda tx: register_claim(tx, _ParamTypes, plan.scope, "winner"))
    if receipt:
        # Admission refusal may now precede the exhausted key, like pause refusal.
        db.typed[KEY_LIMIT_TABLE][("key-hash", 0)]["limit_micro"] = 0
    before = copy.deepcopy((
        db.typed, db.rows, db.reservations, db.gateway_authorizations,
        db.spend_lease_arbitrations, db.spend_lease_open,
    ))
    calls.clear()
    result = billing.authorize_atomic(
        db, _ParamTypes, workspace_id="workspace-1", key_hash="key-hash",
        estimate=500, has_credit_candidate=True, reservation_usage_type="Credits",
        idempotency_scope=plan.scope, idempotency_fingerprint="fingerprint-1",
        expires_at=NOW + timedelta(hours=1), build_auth_body=lambda aid, rid: "{}",
        authorization_id=plan.provisional_id,
        spend_lease_hook=lambda tx, shard: plan.transaction_hook(
            tx, _ParamTypes, "workspace-1", shard,
        ),
        spend_lease_receipt_hash="receipt" if receipt else None,
        credit_escrowed_by_spend_lease=receipt,
    )
    statements = transaction_statements(calls)
    assert any("from tr_trust_event" in sql for sql in statements)
    assert ledger.binds == 0
    if receipt:
        assert result["outcome"] == "admission_rejected:scope_conflict"
        assert not any("tr_key_limit" in sql for sql in statements)
        assert (
            db.typed, db.rows, db.reservations, db.gateway_authorizations,
            db.spend_lease_arbitrations, db.spend_lease_open,
        ) == before
    else:
        authorize_credit_before_key(statements)
        assert result["outcome"] == billing.AuthorizeOutcome.ACCEPTED
        assert result["bound"] is False
        assert db.typed[CREDIT_BALANCE_TABLE][("workspace-1", 0)]["reserved"] == 500
        assert db.typed[KEY_LIMIT_TABLE][("key-hash", 0)]["reserved"] == 500
