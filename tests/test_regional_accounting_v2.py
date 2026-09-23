"""Differential accounting across the V1/V2 deployment boundary."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from tests.fakes.spanner_order import record_statements
from trusted_router import storage_gcp_regional_quota as quota
from trusted_router.regional_quota_ledger import InMemoryRegionalQuotaLedger
from trusted_router.storage_gcp_authorize import SettleOutcome, _finalize_reaped_reservation_atomic
from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE, KEY_LIMIT_TABLE
from trusted_router.storage_gcp_settle_outbox import ENQ_INSERTED, SpannerSettleOutbox
from trusted_router.storage_models import SettleOutboxRow
from trusted_router.types import UsageType


def _totals(db: Any, workspace: str, key: str) -> tuple[int, int, int, int, int]:
    return (
        sum(r["total_usage"] for (ws, _), r in db.typed[CREDIT_BALANCE_TABLE].items() if ws == workspace),
        *(sum((r.get(column) or 0) for (kh, _), r in db.typed[KEY_LIMIT_TABLE].items() if kh == key)
          for column in ("usage", "day_usage", "week_usage", "month_usage")),
    )


def _setup() -> tuple[Any, Any, Any, dict[str, Any]]:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    ws = store.create_workspace("owner", "accounting", trial_credit_microdollars=100_000_000)
    _, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    args = dict(
        workspace_id=ws.id, key_hash=key.hash, key_usage_shards=key.usage_shard_count,
        estimate=10_000, model_id="model", provider="provider", requested_model_id="model",
        candidate_model_ids=["model"], region="us-central1", endpoint_id="provider/model",
        candidate_endpoint_ids=["provider/model"], idempotency_key="request",
        idempotency_fingerprint="f" * 64, tags={},
        expires_at=datetime.now(UTC) + timedelta(hours=2),
    )
    return store, db, key, args


def _authorize(store: Any, args: dict[str, Any]) -> Any:
    outcome, auth = store.authorize_gateway_regional(
        authorization_id="regional", **args, lease_ttl_seconds=60,
        lease_max_microdollars=10_000_000, lease_max_available_basis_points=1000,
        lease_shard_count=16,
    )
    assert outcome == "accepted" and auth is not None
    return auth


def _finalize(store: Any, auth: Any, *, success: bool = True) -> Any:
    return store.typed_finalize_gateway_authorization_result(
        auth.id, success=success, actual_microdollars=7_500,
        selected_usage_type=UsageType.CREDITS,
    )


def test_v2_reaped_then_settled_preserves_terminal_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, key, args = _setup()
    monkeypatch.setattr("trusted_router.storage_gcp.randomized_credit_shards", lambda _n: [7])
    auth = _authorize(store, args)
    # The settle request has loaded ACTIVE authorization before its intent is
    # durable. The reaper can win in precisely this gap.
    loaded = store.get_gateway_authorization(auth.id)
    assert loaded is not None and not loaded.settled
    assert loaded.regional_accounting_version == 2
    ledger = store._regional_quota_ledger
    before = ledger.get(auth.regional_lease_id, region=auth.region)
    assert before.holds[0].key_shard == 7
    reap_now = args["expires_at"] + timedelta(seconds=1)
    monkeypatch.setattr(quota, "utcnow", lambda: reap_now)
    # Settlement happens after the reaper in this timeline, even if the
    # simulated two-hour gap crosses UTC midnight on the test machine.
    monkeypatch.setattr("trusted_router.services.regional_quota_leases._utc_now", lambda: reap_now)
    calls = record_statements(monkeypatch)
    result = _finalize_reaped_reservation_atomic(
        db, store._param_types, reservation_id=loaded.credit_reservation_id,
        reap_now=reap_now, guard_outbox=True, snapshot_booking_enabled=False,
        operational_analytics_outbox=None,
    )
    assert result.outcome == SettleOutcome.SETTLED
    assert not result.snapshot_booked
    statements = [sql for _, sql in calls]
    assert any(sql.startswith("update tr_reservation") for sql in statements)
    assert not any(sql.startswith(("update tr_key_limit", "update tr_credit_balance"))
                   for sql in statements), statements
    reaped = store.get_gateway_authorization(auth.id)
    assert reaped.settled and reaped.finalization_outcome == "refunded"
    assert ledger.get(auth.regional_lease_id, region=auth.region) == before
    assert _totals(db, key.workspace_id, key.hash) == (0,) * 5

    outbox = SpannerSettleOutbox(db, store._param_types)
    assert outbox.enqueue(SettleOutboxRow(
        authorization_id=loaded.id, intent_kind="settle", settle_origin="typed",
        actual_cost_micro=7500, reservation_id=loaded.credit_reservation_id,
        selected_endpoint_id="provider/model", model_id="model",
        selected_usage_type=str(UsageType.CREDITS), settle_body="{}",
    )) == ENQ_INSERTED
    assert db.settle_outbox[(loaded.id, "settle")]["actual_cost_micro"] == 7500
    calls.clear()
    assert not _finalize(store, loaded).finalized
    assert not any(sql.startswith(("update tr_key_limit", "update tr_credit_balance"))
                   for _, sql in calls)
    settled = ledger.get(auth.regional_lease_id, region=auth.region)
    assert settled.holds[0].actual_microdollars == 0
    assert settled.spent_microdollars == 0
    assert _totals(db, key.workspace_id, key.hash) == (0,) * 5

    calls.clear()
    summary = store.reconcile_regional_quota_leases(now=reap_now)
    assert summary == {
        "inspected": 1, "reconciled": 1, "closed": 1, "errors": 0,
        "backlog": 1, "processed": 1, "remaining": 0,
    }
    assert any(sql.startswith("update tr_credit_balance") for _, sql in calls)
    assert not any(sql.startswith("update tr_key_limit") for _, sql in calls)
    assert _totals(db, key.workspace_id, key.hash) == (0,) * 5
    assert not _finalize(store, loaded).finalized
    store.reconcile_regional_quota_leases(now=reap_now + timedelta(minutes=1))
    assert _totals(db, key.workspace_id, key.hash) == (0,) * 5


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("case", ["healthy", "refund", "deleted", "resharded", "unknown"])
def test_regional_accounting_matches_global_once(
    monkeypatch: pytest.MonkeyPatch, version: int, case: str,
) -> None:
    reconcile_now = datetime.now(UTC) + timedelta(minutes=2)
    # Keep the oracle and regional settlement in the same simulated window as
    # import. Separate tests below deliberately cross calendar boundaries.
    # The ordinary typed money path supplies the oracle for the same charge.
    oracle, oracle_db, oracle_key, oracle_args = _setup()
    result, oracle_auth = oracle.authorize_gateway_typed(
        **oracle_args, has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS,
        skip_key_limit=True,
    )
    assert result == "accepted" and oracle_auth is not None
    with monkeypatch.context() as settle_clock:
        settle_clock.setattr("trusted_router.storage_gcp_authorize.utcnow", lambda: reconcile_now)
        assert _finalize(oracle, oracle_auth, success=case != "refund").finalized
    expected = _totals(oracle_db, oracle_key.workspace_id, oracle_key.hash)
    assert expected == ((0,) * 5 if case == "refund" else (7500,) * 5)

    store, db, key, args = _setup()
    # Force a real nonzero Bigtable key shard. V1 reservations historically use 0.
    monkeypatch.setattr("trusted_router.storage_gcp.randomized_credit_shards", lambda _n: [7])
    if version == 1:
        original_grant = quota.grant_regional_quota_lease

        def legacy_grant(*a: Any, **kw: Any) -> Any:
            lease = original_grant(*a, **kw)
            assert lease is not None
            body = json.loads(quota._regional_json_body(lease))
            body.pop("accounting_version")
            store._write_entity("regional_quota_lease", lease.entity_id, body)
            # Exercise the actual missing-version reader, like an already open lease.
            return store._read_entity("regional_quota_lease", lease.entity_id, quota.GlobalRegionalQuotaLease)

        monkeypatch.setattr(quota, "grant_regional_quota_lease", legacy_grant)
    auth = _authorize(store, args)
    assert auth.regional_accounting_version == version
    if version == 1:
        # Existing authorizations also have no version in their durable payload.
        row = db.gateway_authorizations[auth.id]
        payload = json.loads(row["payload"])
        payload.pop("regional_accounting_version")
        row["payload"] = json.dumps(payload)
    persisted = store.get_gateway_authorization(auth.id)
    assert persisted.regional_accounting_version == version
    ledger = store._regional_quota_ledger
    lease_key = (auth.region, auth.regional_lease_id)
    local = ledger.get(auth.regional_lease_id, region=auth.region)
    assert local.holds[0].key_shard == 7
    if case == "unknown":
        with ledger._lock:
            ledger._leases[lease_key] = replace(local, holds=())
    if case == "deleted":
        # Production key deletion removes identity, but preserves accounting rows.
        store.delete_key(key.hash)
    if case == "resharded":
        # Simulate a shrink: consolidate all usage into retained shard zero.
        for identity in list(db.typed[KEY_LIMIT_TABLE]):
            if identity[0] == key.hash and identity[1] != 0:
                del db.typed[KEY_LIMIT_TABLE][identity]

    calls = record_statements(monkeypatch)
    with monkeypatch.context() as settle_clock:
        settle_clock.setattr("trusted_router.storage_gcp_authorize.utcnow", lambda: reconcile_now)
        settle_clock.setattr(
            "trusted_router.services.regional_quota_leases._utc_now", lambda: reconcile_now,
        )
        assert _finalize(store, auth, success=case != "refund").finalized
    statements = [sql for _, sql in calls]
    if version == 2 and case != "unknown":
        assert not any(sql.startswith(("update tr_key_limit", "update tr_credit_balance"))
                       for sql in statements), statements
        assert _totals(db, key.workspace_id, key.hash) == (0,) * 5
    else:
        assert any(sql.startswith("update tr_key_limit") for sql in statements)
        expected_inline = expected[1:]
        assert _totals(db, key.workspace_id, key.hash)[1:] == expected_inline
    if case == "unknown":
        assert _totals(db, key.workspace_id, key.hash) == expected
    assert not _finalize(store, auth, success=case != "refund").finalized

    calls.clear()
    monkeypatch.setattr(quota, "utcnow", lambda: reconcile_now)
    summary = store.reconcile_regional_quota_leases(now=reconcile_now)
    assert summary == {
        "inspected": 1, "reconciled": 1, "closed": 1, "errors": 0,
        "backlog": 1, "processed": 1, "remaining": 0,
    }
    if version == 1:
        assert not any(sql.startswith("update tr_key_limit") for _, sql in calls)
    assert _totals(db, key.workspace_id, key.hash) == expected
    assert not _finalize(store, auth, success=case != "refund").finalized
    store.reconcile_regional_quota_leases(now=reconcile_now + timedelta(minutes=1))
    assert _totals(db, key.workspace_id, key.hash) == expected
    # Retry the original request after settlement/reconciliation: replay, no new charge.
    outcome, replay = store.authorize_gateway_regional(
        authorization_id="retry", **args, lease_ttl_seconds=60,
        lease_max_microdollars=10_000_000, lease_max_available_basis_points=1000,
        lease_shard_count=16,
    )
    assert outcome == "replay" and replay.id == auth.id
    assert _totals(db, key.workspace_id, key.hash) == expected


# Fixed future dates keep these tests independent of the machine's UTC date.
_WINDOW_START = datetime(2027, 1, 1, tzinfo=UTC)


def _window_lease() -> tuple[Any, Any, Any, Any, Any]:
    store, db, key, _ = _setup()
    global_lease = quota.grant_regional_quota_lease(
        store, workspace_id=key.workspace_id, region="us-central1",
        requested_microdollars=1_000_000, per_lease_cap_microdollars=1_000_000,
        max_available_basis_points=1000, ttl_seconds=60,
        minimum_grant_microdollars=10_000, now=_WINDOW_START,
    )
    assert global_lease is not None and global_lease.accounting_version == 2
    global_lease = quota.activate_regional_quota_lease(store, global_lease, now=_WINDOW_START)
    local = quota.regional_lease_from_global(global_lease)
    for hold_id in ("early", "late"):
        local = local.reserve(
            hold_id=hold_id, fingerprint=hold_id, amount_microdollars=10_000,
            fencing_token=local.fencing_token, key_hash=key.hash, key_shard=7,
            now=_WINDOW_START,
        ).lease
    return store, db, key, global_lease, local


def _settle_at(local: Any, hold_id: str, at: datetime, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr("trusted_router.services.regional_quota_leases._utc_now", lambda: at)
    result = local.settle(
        hold_id=hold_id, actual_microdollars=7500, fencing_token=local.fencing_token,
    )
    assert result.hold.settled_at == at
    return result.lease


def _current_totals(db: Any, key: Any, now: datetime) -> tuple[int, ...]:
    """Read counters with the same lazy reset semantics used by key consumers."""
    from trusted_router.spend_windows import window_floors

    floors = window_floors(now)
    rows = [r for (kh, _), r in db.typed[KEY_LIMIT_TABLE].items() if kh == key.hash]
    return (
        sum(r["usage"] for r in rows),
        *(sum((r.get(f"{column}_usage") or 0) if r.get(f"{column}_start") is not None
              and r[f"{column}_start"] >= floors[period] else 0 for r in rows)
          for column, period in (("day", "daily"), ("week", "weekly"), ("month", "monthly"))),
    )


@pytest.mark.parametrize(
    ("settled_at", "import_at", "expected"),
    [
        ("2027-01-05T23:59:00", "2027-01-06T00:01:00", (7500, 0, 7500, 7500)),
        ("2027-01-06T00:00:00", "2027-01-06T00:01:00", (7500, 7500, 7500, 7500)),
        ("2027-01-10T23:59:00", "2027-01-11T00:01:00", (7500, 0, 0, 7500)),
        # April starts Thursday: the month resets while the week continues.
        ("2027-03-31T23:59:00", "2027-04-01T00:01:00", (7500, 0, 7500, 0)),
    ],
    ids=["day-boundary", "same-window-floor-inclusive", "week-boundary", "month-boundary"],
)
def test_window_import_matches_inline_settlement_time(
    monkeypatch: pytest.MonkeyPatch, settled_at: str, import_at: str, expected: tuple[int, ...],
) -> None:
    settled_time = datetime.fromisoformat(settled_at).replace(tzinfo=UTC)
    import_time = datetime.fromisoformat(import_at).replace(tzinfo=UTC)
    store, db, key, global_lease, local = _window_lease()
    local = _settle_at(local, "early", settled_time, monkeypatch)
    monkeypatch.setattr(quota, "utcnow", lambda: import_time)
    result = quota.reconcile_regional_quota_lease(
        store, global_lease, local, close=False, now=import_time,
    )
    assert result.spent_delta_microdollars == 7500
    assert _totals(db, key.workspace_id, key.hash) == (7500, *expected)

    # Differential oracle: ordinary inline settlement at the event time, read
    # after the boundary just as /key and limit enforcement read stale windows.
    oracle, oracle_db, oracle_key, args = _setup()
    monkeypatch.setattr("trusted_router.storage_gcp_authorize.utcnow", lambda: settled_time)
    outcome, auth = oracle.authorize_gateway_typed(
        **args, has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS,
        skip_key_limit=True,
    )
    assert outcome == "accepted" and auth is not None
    assert _finalize(oracle, auth).finalized
    assert _current_totals(oracle_db, oracle_key, import_time) == expected
    assert _current_totals(db, key, import_time) == expected


@pytest.mark.parametrize("late_visibility", [False, True], ids=["one-import", "late-visibility"])
def test_holds_on_both_sides_of_boundary_import_once_even_out_of_order(
    monkeypatch: pytest.MonkeyPatch, late_visibility: bool,
) -> None:
    store, db, key, global_lease, local = _window_lease()
    early_time = datetime(2027, 1, 5, 23, 59, tzinfo=UTC)
    late_time = datetime(2027, 1, 6, tzinfo=UTC)
    monkeypatch.setattr(quota, "utcnow", lambda: late_time)
    # Build two snapshots: the first exposes only the later settlement. The
    # second also exposes the earlier settlement, behind any timestamp cursor.
    local = _settle_at(local, "late", late_time, monkeypatch)
    if late_visibility:
        quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=late_time)
        assert _totals(db, key.workspace_id, key.hash) == (7500,) * 5
    local = _settle_at(local, "early", early_time, monkeypatch)
    quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=late_time)
    expected = (15000, 15000, 7500, 15000, 15000)
    assert _totals(db, key.workspace_id, key.hash) == expected
    quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=late_time)
    assert _totals(db, key.workspace_id, key.hash) == expected


def test_repeated_window_reconciliation_does_not_double_import_across_boundaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, key, global_lease, local = _window_lease()
    at = datetime(2027, 1, 5, 23, 59, tzinfo=UTC)
    local = _settle_at(local, "early", at, monkeypatch)
    monkeypatch.setattr(quota, "utcnow", lambda: at)
    quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=at)
    assert _totals(db, key.workspace_id, key.hash) == (7500,) * 5
    # Reuse the stale global object: the guard must come from the transaction's
    # durable lease read, not the caller's snapshot (including at close).
    for now in (at, at + timedelta(minutes=2)):
        monkeypatch.setattr(quota, "utcnow", lambda now=now: now)
        quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=now)
        assert _totals(db, key.workspace_id, key.hash) == (7500,) * 5
    next_day = at + timedelta(minutes=2)
    local = _settle_at(local, "late", next_day, monkeypatch)
    monkeypatch.setattr(quota, "utcnow", lambda: next_day)
    quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=next_day)
    assert _totals(db, key.workspace_id, key.hash) == (15000, 15000, 7500, 15000, 15000)
    for now in (next_day, datetime(2027, 2, 1, tzinfo=UTC)):
        monkeypatch.setattr(quota, "utcnow", lambda now=now: now)
        quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=now)
        assert _current_totals(db, key, now) == (
            (15000, 7500, 15000, 15000) if now == next_day else (15000, 0, 0, 0)
        )
    quota.reconcile_regional_quota_lease(store, global_lease, local, close=True, now=now)
    assert _totals(db, key.workspace_id, key.hash) == (15000, 15000, 7500, 15000, 15000)


def test_legacy_settled_hold_without_timestamp_uses_first_import_time_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.services.regional_quota_leases import HoldState

    store, db, key, global_lease, local = _window_lease()
    local = replace(local, holds=(
        replace(local.holds[0], state=HoldState.SETTLED, actual_microdollars=7500),
        local.holds[1],
    ))
    now = datetime(2027, 4, 1, tzinfo=UTC)
    monkeypatch.setattr(quota, "utcnow", lambda: now)
    quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=now)
    assert _totals(db, key.workspace_id, key.hash) == (7500,) * 5
    quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=now)
    assert _totals(db, key.workspace_id, key.hash) == (7500,) * 5
    monkeypatch.setattr(quota, "utcnow", lambda: now + timedelta(days=1))
    quota.reconcile_regional_quota_lease(
        store, global_lease, local, close=False, now=now + timedelta(days=1),
    )
    assert _current_totals(db, key, now + timedelta(days=1)) == (7500, 0, 7500, 7500)


def test_hold_settled_after_batch_start_uses_import_transaction_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, key, global_lease, local = _window_lease()
    batch_time = datetime(2027, 1, 5, 23, 59, 59, tzinfo=UTC)
    settled_time = batch_time + timedelta(seconds=2)
    import_time = settled_time + timedelta(seconds=1)
    local = _settle_at(local, "early", settled_time, monkeypatch)
    monkeypatch.setattr(quota, "utcnow", lambda: import_time)
    quota.reconcile_regional_quota_lease(
        store, global_lease, local, close=False, now=batch_time,
    )
    assert _current_totals(db, key, import_time) == (7500,) * 4
    # Once marked imported, a later pass cannot repair a missed day increment.
    quota.reconcile_regional_quota_lease(
        store, global_lease, local, close=False, now=import_time,
    )
    assert _current_totals(db, key, import_time) == (7500,) * 4


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("2027-01-05T23:59:59", "2027-01-06T00:00:01", (7600, 100, 7600, 7600)),
        ("2027-01-10T23:59:59", "2027-01-11T00:00:01", (7600, 100, 100, 7600)),
        ("2027-03-31T23:59:59", "2027-04-01T00:00:01", (7600, 100, 7600, 100)),
    ],
    ids=["daily", "weekly", "monthly"],
)
@pytest.mark.parametrize("resharded", [False, True], ids=["original-shard", "fallback-shard"])
def test_newer_stored_window_retries_with_fresh_attribution(
    monkeypatch: pytest.MonkeyPatch, before: str, after: str,
    expected: tuple[int, ...], resharded: bool,
) -> None:
    from trusted_router.spend_windows import window_floors
    from trusted_router.storage_gcp_counter_dml import release_key

    store, db, key, global_lease, local = _window_lease()
    old_time = datetime.fromisoformat(before).replace(tzinfo=UTC)
    new_time = datetime.fromisoformat(after).replace(tzinfo=UTC)
    local = _settle_at(local, "early", old_time, monkeypatch)
    if resharded:
        for identity in list(db.typed[KEY_LIMIT_TABLE]):
            if identity[0] == key.hash and identity[1] != 0:
                del db.typed[KEY_LIMIT_TABLE][identity]
    shard = 0 if resharded else 7
    # A real inline counter update has already advanced the target window.
    assert store._run_in_transaction(lambda tx: release_key(
        tx, store._param_types, key.hash, 0, 100, book_to_byok=False,
        window_floors=window_floors(new_time), shard=shard,
    )) == 1
    # Isolate each stored-floor comparison: the other two may still be stale
    # on a lazily initialized counter row. Omitting any one guard must fail.
    column = {"daily": "day", "weekly": "week", "monthly": "month"}[
        "monthly" if old_time.month != new_time.month
        else "weekly" if old_time.weekday() == 6 else "daily"
    ]
    for other in ("day", "week", "month"):
        if other != column:
            db.typed[KEY_LIMIT_TABLE][(key.hash, shard)][f"{other}_start"] = None
            db.typed[KEY_LIMIT_TABLE][(key.hash, shard)][f"{other}_usage"] = 0
    attempts = []

    def clock() -> datetime:
        at = old_time if not attempts else new_time
        attempts.append(at)
        return at

    monkeypatch.setattr(quota, "utcnow", clock)
    quota.reconcile_regional_quota_lease(
        store, global_lease, local, close=False, now=old_time,
    )
    assert attempts == [old_time, new_time]
    # Non-target windows had no inline usage in the isolated fixture.
    adjusted = list(expected)
    for index, other in enumerate(("day", "week", "month"), 1):
        if other != column:
            adjusted[index] -= 100
    assert _current_totals(db, key, new_time) == tuple(adjusted)
    assert _totals(db, key.workspace_id, key.hash)[0] == 7500
    quota.reconcile_regional_quota_lease(
        store, global_lease, local, close=False, now=new_time,
    )
    assert _current_totals(db, key, new_time) == tuple(adjusted)


def test_window_hold_ids_retained_while_active_and_cleared_at_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, key, global_lease, local = _window_lease()
    at = datetime(2027, 1, 5, tzinfo=UTC)
    monkeypatch.setattr(quota, "utcnow", lambda: at)
    local = _settle_at(local, "early", at, monkeypatch)
    quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=at)

    def read_lease() -> Any:
        return quota.get_global_regional_quota_lease(
            store, workspace_id=global_lease.workspace_id, region=global_lease.region,
            lease_id=global_lease.lease_id,
        )

    assert read_lease().state == "active"
    assert read_lease().reconciled_window_hold_ids == ["early"]
    local = _settle_at(local, "late", at, monkeypatch)
    quota.reconcile_regional_quota_lease(store, global_lease, local, close=False, now=at)
    assert read_lease().reconciled_window_hold_ids == ["early", "late"]
    quota.reconcile_regional_quota_lease(store, global_lease, local, close=True, now=at)
    assert read_lease().state == "closed"
    assert read_lease().reconciled_window_hold_ids == []
    replay = quota.reconcile_regional_quota_lease(
        store, global_lease, local, close=True, now=at,
    )
    assert replay.replayed and replay.closed
    assert read_lease().reconciled_window_hold_ids == []
    assert _totals(db, key.workspace_id, key.hash) == (15000,) * 5
