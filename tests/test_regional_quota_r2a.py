"""Regression proofs for bounded regional escrow, worker capacity, and refunds."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from google.api_core.exceptions import DeadlineExceeded

from tests.fakes.spanner import _FakeTransaction, make_fake_store
from tests.test_trust_eligibility_pr2 import arm_store, regional_args, workspace_state
from trusted_router import storage_gcp_regional_quota as quota
from trusted_router.config import Settings
from trusted_router.regional_quota_ledger import InMemoryRegionalQuotaLedger
from trusted_router.services.regional_quota_leases import HoldState
from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle
from trusted_router.storage import configure_store
from trusted_router.storage_models import CreditAccount, SettleOutboxRow
from trusted_router.types import UsageType


def _authorized() -> tuple[Any, Any, Any, Any]:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    ledger = InMemoryRegionalQuotaLedger()
    store._regional_quota_ledger = ledger
    ws = store.create_workspace("owner", "r2a", trial_credit_microdollars=200_000_000)
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    outcome, auth = store.authorize_gateway_regional(
        authorization_id="r2a-auth", **regional_args(ws.id, key),
    )
    assert outcome == "accepted" and auth is not None
    return store, db, ledger, auth


def test_authorize_read_volume_is_independent_of_ten_thousand_closed_leases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    ws = store.create_workspace("owner", "bounded", trial_credit_microdollars=200_000_000)
    workspace_state(db, 3, ws.id)
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    args = regional_args(ws.id, key)
    outcome, auth = store.authorize_gateway_regional(authorization_id="first", **args)
    assert outcome == "accepted" and auth is not None
    lease = store._list_entities("regional_quota_lease", cls=quota.GlobalRegionalQuotaLease)[0]
    reads: list[tuple[str, int]] = []
    original = _FakeTransaction.execute_sql

    def read(tx: Any, sql: str, **kwargs: Any) -> Any:
        rows = list(original(tx, sql, **kwargs))
        reads.append((sql, len(rows)))
        return rows

    monkeypatch.setattr(_FakeTransaction, "execute_sql", read)

    def authorize(name: str) -> list[tuple[str, int]]:
        reads.clear()
        outcome, _ = store.authorize_gateway_regional(
            authorization_id=name, **{**args, "idempotency_key": name},
        )
        assert outcome == "accepted"
        return list(reads)

    before = authorize("before")
    for index in range(10_000):
        closed = replace(lease, lease_id=f"closed-{index}", state="closed")
        store._write_entity("regional_quota_lease", closed.entity_id, closed)
    after = authorize("after")
    assert not any("id IN UNNEST(@ids)" in sql for sql, _ in after)
    assert sum(count for _, count in after) == sum(count for _, count in before)
    assert len(after) == len(before)
    assert not any("WHERE kind='regional_quota_lease'" in sql for sql, _ in after)


@pytest.mark.parametrize("legacy_writer", [False, True])
def test_concurrent_grants_on_different_credit_shards_fit_only_one_pool(
    monkeypatch: pytest.MonkeyPatch, legacy_writer: bool,
) -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    credit = workspace_state(db, 1)
    store._write_entity("credit", "workspace", CreditAccount(workspace_id="workspace", shard_count=3))
    original_read = _FakeTransaction.execute_sql

    def read(tx: Any, sql: str, **kwargs: Any) -> Any:
        before = set(tx.read_versions)
        rows = original_read(tx, sql, **kwargs)
        if sql.startswith("SELECT shard, trust_tier"):
            # Trust is immutable in this test. The fake tracks whole rows, but
            # Spanner reads just these trust columns, not reserved/total_usage.
            # Remove that incidental fake conflict so only the escrow range
            # can serialize grants on disjoint credit counters.
            for key in set(tx.read_versions) - before:
                if key[0] == "typed" and key[1] == "tr_credit_balance":
                    del tx.read_versions[key]
        return rows

    monkeypatch.setattr(_FakeTransaction, "execute_sql", read)
    if legacy_writer:
        insert = quota.insert_entity_dml_at

        def legacy_insert(tx: Any, types: Any, kind: str, *args: Any) -> Any:
            # Simulate pre-deploy writers which maintain only the expiry index
            # and fence. Empty new-index ranges cannot serialize their grants.
            if kind != "regional_quota_lease_workspace_open":
                return insert(tx, types, kind, *args)
            return None

        monkeypatch.setattr(quota, "insert_entity_dml_at", legacy_insert)
    for shard in (1, 2):
        db.typed["tr_credit_balance"][("workspace", shard)] = {**credit, "shard": shard}
    context = threading.local()
    # Force different counter rows: a shared credit write must not accidentally
    # provide the serialization that this test is meant to prove.
    monkeypatch.setattr(quota, "_credit_rows", lambda *_: [(context.shard, 2_000_000_000, 0, 0)])
    db._ready_barrier = threading.Barrier(2)

    def grant(shard: int) -> Any:
        context.shard = shard
        return quota.grant_regional_quota_lease(
            store, workspace_id="workspace", region=f"region-{shard}", quota_shard=shard,
            requested_microdollars=5_000_000, per_lease_cap_microdollars=5_000_000,
            max_available_basis_points=1000, minimum_grant_microdollars=5_000_000,
            ttl_seconds=300,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        grants = list(executor.map(grant, (1, 2)))
    assert len([lease for lease in grants if lease is not None]) == 1
    assert sum(row["reserved"] for row in db.typed["tr_credit_balance"].values()) == 5_000_000
    assert db.aborts >= 1


def _pending(store: Any, workspace: str, region: str, shard: int) -> quota.OpenRegionalQuotaLease:
    lease = quota.GlobalRegionalQuotaLease(
        lease_id=f"{workspace}-{region}-{shard}", workspace_id=workspace, region=region,
        quota_shard=shard, fencing_token=1, granted_microdollars=1000, credit_shard=0,
        expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
    )
    index = quota.OpenRegionalQuotaLease(
        lease.entity_id, workspace, region, lease.lease_id, lease.expires_at,
    )
    store._write_entity("regional_quota_lease", lease.entity_id, lease)
    store._write_entity("regional_quota_lease_open", index.entity_id, index)
    return index


@pytest.mark.parametrize("limit", [81, 157, 500])
def test_worker_capacity_honors_configured_budget_above_closure_arrival_rate(limit: int) -> None:
    store, _db, _ = make_fake_store(request_record_write_mode="typed")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    for workspace in range(5):
        for region in range(5):
            for shard in range(16):
                _pending(store, str(workspace), str(region), shard)
    settings = Settings(environment="test", regional_quota_reconcile_limit=limit)
    result = store.reconcile_regional_quota_leases(limit=settings.regional_quota_reconcile_limit)
    assert result["errors"] == 0
    assert result["backlog"] == 400
    assert result["processed"] == result["inspected"] == min(limit, 400)
    assert result["remaining"] == max(400 - limit, 0)
    assert Settings(environment="test").regional_quota_reconcile_limit == 500


def test_cursor_fairly_paginates_workspaces_regions_and_pending_leases() -> None:
    store, _db, _ = make_fake_store(request_record_write_mode="typed")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    # One workspace has far more regions/leases, all pending, and none close.
    for region in range(5):
        for shard in range(16):
            _pending(store, "a", str(region), shard)
    for shard in range(2):
        _pending(store, "b", "0", shard)
    visited = []
    for _ in range(20):
        result = store.reconcile_regional_quota_leases(limit=1)
        assert result["processed"] == 1 and result["remaining"] == 81
        cursor = store._read_entity(
            "regional_quota_reconciler_cursor", "singleton", quota.RegionalReconcileCursor,
        )
        workspace = cursor.workspace
        region = cursor.regions[workspace]
        visited.append((workspace, region, cursor.leases[f"{workspace}#{region}"]))
    assert [workspace for workspace, _, _ in visited] == ["a", "b"] * 10
    a_visits = [entry for entry in visited if entry[0] == "a"]
    assert [region for _, region, _ in a_visits] == list("01234") * 2
    assert len({lease for _, _, lease in a_visits}) == 10
    assert len({lease for ws, _, lease in visited if ws == "b"}) == 2


def test_worker_time_budget_preserves_unvisited_backlog_and_cursor() -> None:
    store, _db, _ = make_fake_store(request_record_write_mode="typed")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    for shard in range(100):
        _pending(store, "workspace", "region", shard)
    stopped = store.reconcile_regional_quota_leases(limit=500, max_seconds=0)
    assert stopped["processed"] == 0 and stopped["remaining"] == 100
    resumed = store.reconcile_regional_quota_leases(limit=81)
    assert resumed["processed"] == 81 and resumed["remaining"] == 19


@pytest.mark.parametrize("intent_timing", ["before_scan", "after_scan", "after_reconcile"])
def test_expired_hold_survives_settle_intent_on_either_side_of_scan(
    intent_timing: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, ledger, auth = _authorized()
    row = SettleOutboxRow(
        authorization_id=auth.id, reservation_id=auth.credit_reservation_id,
        intent_kind="settle", settle_origin="typed", actual_cost_micro=7500,
        selected_endpoint_id="provider/model", model_id="model", selected_usage_type="Credits",
        settle_body='{"authorization_id":"r2a-auth","status":"success"}',
    )
    enqueued = False

    def enqueue() -> None:
        nonlocal enqueued
        if not enqueued:
            store.settle_outbox.enqueue(row)
            enqueued = True

    if intent_timing == "before_scan":
        enqueue()
    original = type(ledger).get

    def scan(self: Any, *args: Any, **kwargs: Any) -> Any:
        local = original(self, *args, **kwargs)
        if intent_timing == "after_scan":
            enqueue()
        return local

    monkeypatch.setattr(type(ledger), "get", scan)
    later = datetime.now(UTC) + timedelta(hours=3)
    result = store.reconcile_regional_quota_leases(now=later)
    assert result["errors"] == 0 and result["closed"] == 0
    local = ledger.get(auth.regional_lease_id, region=auth.region)
    assert local.holds[0].state == HoldState.RESERVED
    if intent_timing == "after_reconcile":
        enqueue()
    configure_store(store)
    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    result = store.reconcile_regional_quota_leases(now=later)
    assert result["errors"] == 0 and result["closed"] == 1
    credits = [r for (ws, _), r in db.typed["tr_credit_balance"].items() if ws == auth.workspace_id]
    assert sum(r["total_usage"] for r in credits) == 7500
    assert sum(r["reserved"] for r in credits) == 0
    assert ledger.get(auth.regional_lease_id, region=auth.region).spent_microdollars == 7500


@pytest.mark.parametrize("actual", [0, 7500])
def test_reconciler_recovers_only_durable_terminal_amount(
    actual: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, ledger, auth = _authorized()
    # Simulate a typed terminal result with a local hold still needing repair.
    with monkeypatch.context() as patch:
        patch.setattr(type(store), "_finalize_regional_quota_hold", lambda *_a, **_kw: None)
        result = store.typed_finalize_gateway_authorization_result(
            auth.id, success=actual > 0, actual_microdollars=actual,
            selected_usage_type=UsageType.CREDITS,
        )
        assert result.finalized
    result = store.reconcile_regional_quota_leases(now=datetime.now(UTC) + timedelta(hours=3))
    assert result["closed"] == 1 and result["errors"] == 0
    local = ledger.get(auth.regional_lease_id, region=auth.region)
    assert local.spent_microdollars == actual
    assert local.holds[0].state == (HoldState.SETTLED if actual else HoldState.REFUNDED)
    assert sum(row["total_usage"] for row in db.typed["tr_credit_balance"].values()) == actual


def test_failing_workspace_does_not_starve_another_workspace() -> None:
    store, _db, _ = make_fake_store(request_record_write_mode="typed")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    broken = _pending(store, "a", "region", 0)
    _pending(store, "b", "region", 0)
    # Keep a broken index at the front of every ordinary prefix scan.
    _db.rows.pop(("regional_quota_lease", broken.lease_entity_id))
    first = store.reconcile_regional_quota_leases(limit=1)
    second = store.reconcile_regional_quota_leases(limit=1)
    assert first["errors"] == 1
    assert second["errors"] == 0 and second["processed"] == 1
    cursor = store._read_entity(
        "regional_quota_reconciler_cursor", "singleton", quota.RegionalReconcileCursor,
    )
    assert cursor.workspace == "b"


@pytest.mark.parametrize("status", ["pending", "dead"])
def test_terminal_zero_with_guarded_intent_retains_local_hold(
    status: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, ledger, auth = _authorized()
    with monkeypatch.context() as patch:
        patch.setattr(type(store), "_finalize_regional_quota_hold", lambda *_a, **_kw: None)
        assert store.typed_finalize_gateway_authorization_result(
            auth.id, success=False, actual_microdollars=0,
            selected_usage_type=UsageType.CREDITS,
        ).finalized
    row = SettleOutboxRow(
        authorization_id=auth.id, reservation_id=auth.credit_reservation_id,
        intent_kind="settle", settle_origin="typed", actual_cost_micro=7500,
        settle_body='{"authorization_id":"r2a-auth"}',
    )
    store.settle_outbox.enqueue(row)
    db.settle_outbox[(auth.id, "settle")]["status"] = status
    result = store.reconcile_regional_quota_leases(now=datetime.now(UTC) + timedelta(hours=3))
    assert result["closed"] == 0 and result["errors"] == 0
    assert ledger.get(auth.regional_lease_id, region=auth.region).holds[0].state == HoldState.RESERVED


def _grant(store: Any, workspace: str, shard: int = 0, amount: int = 5_000_000) -> Any:
    return quota.grant_regional_quota_lease(
        store, workspace_id=workspace, region="us-central1", quota_shard=shard,
        requested_microdollars=amount, per_lease_cap_microdollars=amount,
        max_available_basis_points=1000, minimum_grant_microdollars=1, ttl_seconds=300,
    )


def test_grant_reads_and_locks_only_its_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    for workspace in ("workspace-a", "workspace-b"):
        workspace_state(db, 3, workspace)
        assert _grant(store, workspace, amount=1_000_000) is not None
    statements = []
    transactions = []
    original = _FakeTransaction.execute_sql

    def read(tx: Any, sql: str, **kwargs: Any) -> Any:
        statements.append((sql, kwargs.get("params", {})))
        transactions.append(tx)
        return original(tx, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "execute_sql", read)
    assert _grant(store, "workspace-a", shard=1, amount=1_000_000) is not None
    escrow_reads = [(sql, p) for sql, p in statements
                    if str(p.get("kind", "")).startswith("regional_quota_")]
    assert escrow_reads
    for sql, params in escrow_reads:
        assert params["kind"] != "regional_quota_lease_open"
        if "id=@id" in sql:
            assert params["id"].startswith("workspace-a#")
        elif "id IN UNNEST(@ids)" in sql:
            assert all(eid.startswith("workspace-a#") for eid in params["ids"])
        else:
            assert "STARTS_WITH(id, @prefix)" in sql
            assert params["prefix"] == "workspace-a#"
            assert params["kind"] != "regional_quota_lease"  # no history
    tx = transactions[-1]
    assert ("regional_quota_lease_workspace_open", "workspace-a#") in tx.entity_prefix_reads
    assert ("regional_quota_fence", "workspace-a#") in tx.entity_prefix_reads
    assert not any("workspace-b" in str(k) or k[0] == "entity_kind" for k in tx.read_versions)


def test_different_workspace_grants_do_not_conflict() -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    for workspace in ("workspace-a", "workspace-b"):
        workspace_state(db, 1, workspace)
    db._ready_barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        leases = list(executor.map(lambda ws: _grant(store, ws), ("workspace-a", "workspace-b")))
    assert all(lease is not None for lease in leases)
    assert db.aborts == 0


def test_authorize_does_not_read_other_workspaces_open_escrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    ws = store.create_workspace("owner", "authorize-cost", trial_credit_microdollars=200_000_000)
    workspace_state(db, 3, ws.id)
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    args = regional_args(ws.id, key)
    outcome, _ = store.authorize_gateway_regional(authorization_id="seed", **args)
    assert outcome == "accepted"
    reads = []
    original = _FakeTransaction.execute_sql

    def read(tx: Any, sql: str, **kwargs: Any) -> Any:
        rows = list(original(tx, sql, **kwargs))
        reads.append((sql, kwargs.get("params", {}), len(rows)))
        return rows

    monkeypatch.setattr(_FakeTransaction, "execute_sql", read)

    def authorize(name: str) -> list[Any]:
        reads.clear()
        outcome, _ = store.authorize_gateway_regional(
            authorization_id=name, **{**args, "idempotency_key": name},
        )
        assert outcome == "accepted"
        return list(reads)

    before = authorize("before-open")
    for n in range(1000):
        index = _pending(store, f"other-{n}", "us-central1", 0)
        store._write_entity("regional_quota_lease_workspace_open", index.lease_entity_id, index)
    after = authorize("after-open")
    assert len(after) == len(before)
    assert sum(count for _, _, count in after) == sum(count for _, _, count in before)
    for sql, params, _ in after:
        if str(params.get("kind", "")).startswith("regional_quota_"):
            assert "id=@id" in sql
            if params["kind"] == "regional_quota_hold_cancellation":
                assert params["id"] == "after-open"
                continue
            assert params["id"].startswith(f"{ws.id}#")
            assert params["kind"] in {"regional_quota_lease", "regional_quota_fence"}


@pytest.mark.parametrize("state", ["pending", "active", "draining", "quarantined"])
def test_transition_counts_legacy_owned_leases_without_workspace_index(state: str) -> None:
    import json

    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    workspace_state(db, 1)
    legacy = _grant(store, "workspace", amount=4_000_000)
    assert legacy is not None
    # The exact pre-deploy schema: expiry index + fence, no workspace index or
    # issuance bound. Expiry/quarantine/drain do not release the owned money.
    db.rows.pop(("regional_quota_lease_workspace_open", legacy.entity_id))
    record = db.rows[("regional_quota_lease", legacy.entity_id)]
    body = json.loads(record.body)
    body.pop("issuance_pool_micro")
    body.update(state=state, expires_at=(datetime.now(UTC) - timedelta(hours=1)).isoformat())
    record.body = json.dumps(body)
    assert store._run_in_transaction(
        lambda tx: quota._active_regional_escrow(tx, store._param_types, "workspace")
    ) == 4_000_000
    next_lease = _grant(store, "workspace", shard=1)
    assert next_lease.granted_microdollars == 1_000_000
    assert db.typed["tr_credit_balance"][("workspace", 0)]["reserved"] == 5_000_000
    assert _grant(store, "workspace", shard=2) is None


def test_workspace_index_retains_quarantine_and_is_deleted_atomically_on_close() -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    workspace_state(db, 1)
    lease = _grant(store, "workspace")
    key = ("regional_quota_lease_workspace_open", lease.entity_id)
    assert key in db.rows
    quota.quarantine_regional_quota_lease(store, lease, reason="test")
    assert key in db.rows
    assert _grant(store, "workspace", shard=1) is None
    local = quota.regional_lease_from_global(lease).begin_drain(fencing_token=lease.fencing_token)
    assert quota.reconcile_regional_quota_lease(store, lease, local, close=True).closed
    assert key not in db.rows
    assert ("regional_quota_lease_open", quota._open_lease_entity_id(lease)) not in db.rows
    assert _grant(store, "workspace", shard=1) is not None


def test_pool_increase_waits_for_outstanding_smaller_bound_to_close() -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    settings = arm_store(store, db)
    workspace_state(db, 3)
    settings.regional_quota_lease_max_microdollars = 5_000_000
    first = _grant(store, "workspace", amount=4_000_000)
    settings.regional_quota_lease_max_microdollars = 10_000_000
    second = _grant(store, "workspace", shard=1)
    assert first.issuance_pool_micro == second.issuance_pool_micro == 5_000_000
    assert second.granted_microdollars == 1_000_000


def test_grant_prunes_workspace_pointer_left_by_legacy_close() -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    workspace_state(db, 1)
    lease = _grant(store, "workspace")
    key = ("regional_quota_lease_workspace_open", lease.entity_id)
    stale_index = db.rows[key]
    local = quota.regional_lease_from_global(lease).begin_drain(fencing_token=lease.fencing_token)
    assert quota.reconcile_regional_quota_lease(store, lease, local, close=True).closed
    # A pre-deploy reconciler removes only the expiry index and active fence.
    db.rows[key] = stale_index
    assert _grant(store, "workspace") is not None
    assert key not in db.rows


def _orphan(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any, Any, Any]:
    """Kill the writer after local reserve commits, before either typed insert."""
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    ledger = InMemoryRegionalQuotaLedger()
    store._regional_quota_ledger = ledger
    ws = store.create_workspace("owner", "orphan", trial_credit_microdollars=200_000_000)
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    captured = []

    def crash(*_args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs["authorization"])
        raise SystemExit("crash after reserve commit")

    with monkeypatch.context() as patch:
        patch.setattr(quota, "record_regional_gateway_authorization", crash)
        with pytest.raises(SystemExit):
            store.authorize_gateway_regional(
                authorization_id="orphan", **regional_args(ws.id, key),
            )
    assert not db.reservations and not db.gateway_authorizations
    assert ledger.get(captured[0].regional_lease_id, region=captured[0].region).reserved_microdollars == 10_000
    return store, db, ledger, captured[0]


def _record(store: Any, auth: Any) -> dict[str, Any]:
    return quota.record_regional_gateway_authorization(
        store, authorization=auth, idempotency_scope=None, idempotency_fingerprint=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=10),
    )


@pytest.mark.parametrize("days", [1, 30])
def test_crash_before_typed_record_is_cancelled_before_refund_and_releases_grant(
    monkeypatch: pytest.MonkeyPatch, days: int,
) -> None:
    store, db, ledger, auth = _orphan(monkeypatch)
    now = datetime.now(UTC) + timedelta(days=days)
    original = type(ledger).refund
    refunds = []

    def refund(self: Any, *args: Any, **kwargs: Any) -> Any:
        # A mutation returning zero without committing cancellation fails here.
        tombstone = store._read_entity(
            "regional_quota_hold_cancellation", auth.id, quota.RegionalHoldCancellation,
        )
        assert tombstone == quota.RegionalHoldCancellation(
            auth.id, auth.workspace_id, auth.regional_lease_id, auth.region,
            auth.regional_fencing_token, quota._iso(now),
        )
        refunds.append(auth.id)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(ledger), "refund", refund)
    result = store.reconcile_regional_quota_leases(now=now)
    assert result["closed"] == 1 and result["errors"] == 0
    assert refunds == [auth.id]
    local = ledger.get(auth.regional_lease_id, region=auth.region)
    assert local.state.value == "closed" and local.reserved_microdollars == 0
    assert local.holds[0].state == HoldState.REFUNDED
    assert sum(row["reserved"] for row in db.typed["tr_credit_balance"].values()) == 0
    assert not db.reservations and not db.gateway_authorizations
    assert store.reconcile_regional_quota_leases(now=now)["processed"] == 0
    assert refunds == [auth.id]
    assert _record(store, auth)["outcome"] == "cancelled"
    assert not db.reservations and not db.gateway_authorizations


@pytest.mark.parametrize("refund_fails", [False, True])
def test_delayed_writer_is_refused_compensated_and_returns_typed_fallback(
    monkeypatch: pytest.MonkeyPatch, refund_fails: bool, caplog: Any,
) -> None:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    ledger = InMemoryRegionalQuotaLedger()
    store._regional_quota_ledger = ledger
    ws = store.create_workspace("owner", "delayed", trial_credit_microdollars=200_000_000)
    _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    original = quota.record_regional_gateway_authorization
    captured = []
    later = datetime.now(UTC) + timedelta(hours=3)

    def delayed(*args: Any, **kwargs: Any) -> Any:
        captured.append(kwargs["authorization"])
        with monkeypatch.context() as patch:
            if refund_fails:
                def failed_refund(*_args: Any, **_kwargs: Any) -> Any:
                    raise RuntimeError("lost Bigtable connection after cancellation")
                patch.setattr(type(ledger), "refund", failed_refund)
            result = store.reconcile_regional_quota_leases(now=later)
        assert result["errors"] == int(refund_fails)
        assert result["closed"] == int(not refund_fails)
        return original(*args, **kwargs)

    monkeypatch.setattr(quota, "record_regional_gateway_authorization", delayed)
    outcome, auth = store.authorize_gateway_regional(
        authorization_id="delayed", **regional_args(ws.id, key),
    )
    assert outcome == "unavailable" and auth is None
    assert not db.reservations and not db.gateway_authorizations
    local = ledger.get(captured[0].regional_lease_id, region=captured[0].region)
    assert local.reserved_microdollars == 0 and local.holds[0].state == HoldState.REFUNDED
    assert "compensation failed" not in caplog.text
    assert store.reconcile_regional_quota_leases(now=later)["errors"] == 0
    assert sum(row["reserved"] for row in db.typed["tr_credit_balance"].values()) == 0


@pytest.mark.parametrize("existing", ["authorization", "reservation", "pending", "dead"])
@pytest.mark.parametrize("timing", ["before", "during"])
def test_orphan_cancellation_transaction_rechecks_all_three_absences(
    monkeypatch: pytest.MonkeyPatch, existing: str, timing: str,
) -> None:
    store, db, ledger, auth = _orphan(monkeypatch)
    inserted = False

    def insert() -> None:
        nonlocal inserted
        inserted = True
        if existing in {"authorization", "reservation"}:
            with monkeypatch.context() as patch:
                if existing == "reservation":
                    patch.setattr(quota, "insert_gateway_authorization", lambda *_a, **_kw: None)
                assert _record(store, auth)["outcome"] == "accepted"
        else:
            store.settle_outbox.enqueue(SettleOutboxRow(
                authorization_id=auth.id, reservation_id="unknown", intent_kind="settle",
                settle_origin="typed", actual_cost_micro=7500, settle_body="{}",
            ))
            db.settle_outbox[(auth.id, "settle")]["status"] = existing

    original = quota._insert_entity_dml

    def cancel_insert(*args: Any, **kwargs: Any) -> None:
        if not inserted:
            # All initial absence reads have completed, but cancellation has
            # not committed. Spanner must abort/retry after this competing write.
            insert()
        original(*args, **kwargs)

    if timing == "before":
        insert()
    else:
        monkeypatch.setattr(quota, "_insert_entity_dml", cancel_insert)
    result = store.reconcile_regional_quota_leases(now=datetime.now(UTC) + timedelta(days=1))
    assert result["errors"] == 0 and result["closed"] == 0
    assert inserted
    assert ("regional_quota_hold_cancellation", auth.id) not in db.rows
    assert ledger.get(auth.regional_lease_id, region=auth.region).reserved_microdollars == 10_000
    if timing == "during":
        assert db.aborts >= 1


def test_cancellation_commit_failure_never_refunds_and_retry_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, ledger, auth = _orphan(monkeypatch)
    now = datetime.now(UTC) + timedelta(days=1)
    original = quota._insert_entity_dml

    def fail(*args: Any, **kwargs: Any) -> None:
        original(*args, **kwargs)
        raise RuntimeError("transaction aborted before commit")

    with monkeypatch.context() as patch:
        patch.setattr(quota, "_insert_entity_dml", fail)
        assert store.reconcile_regional_quota_leases(now=now)["errors"] == 1
    assert ("regional_quota_hold_cancellation", auth.id) not in db.rows
    assert ledger.get(auth.regional_lease_id, region=auth.region).reserved_microdollars == 10_000
    assert store.reconcile_regional_quota_leases(now=now)["closed"] == 1
    assert store.reconcile_regional_quota_leases(now=now)["closed"] == 0


def test_unexpired_orphan_is_not_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    store, db, ledger, auth = _orphan(monkeypatch)
    result = store.reconcile_regional_quota_leases()
    assert result["errors"] == 0 and result["closed"] == 0
    assert ("regional_quota_hold_cancellation", auth.id) not in db.rows
    assert ledger.get(auth.regional_lease_id, region=auth.region).reserved_microdollars == 10_000


@pytest.mark.parametrize("transient", [True, False])
def test_hold_lookup_failure_preserves_reservation_and_allows_transient_progress(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, transient: bool,
) -> None:
    store, db, ledger, auth = _authorized()
    lease = store._list_entities("regional_quota_lease", cls=quota.GlobalRegionalQuotaLease)[0]
    # This terminal typed hold still needs local settlement, behind the failing lookup.
    with monkeypatch.context() as patch:
        patch.setattr(type(store), "_finalize_regional_quota_hold", lambda *_a, **_kw: None)
        assert store.typed_finalize_gateway_authorization_result(
            auth.id, success=True, actual_microdollars=7500,
            selected_usage_type=UsageType.CREDITS,
        ).finalized
    ledger.reserve(
        lease.lease_id, region=lease.region, hold_id="a-failing", fingerprint="a-failing",
        amount_microdollars=10_000, fencing_token=lease.fencing_token,
        key_hash=auth.key_hash, key_shard=0,
        hold_expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    attempted = []
    original = quota.terminal_regional_hold_amount

    def failing(*args: Any, **kwargs: Any) -> Any:
        attempted.append(args[2])
        if args[2] == "a-failing":
            if transient:
                raise DeadlineExceeded("hold lookup timed out")
            raise ValueError("hold lookup programming error")
        return original(*args, **kwargs)

    later = datetime.now(UTC) + timedelta(hours=3)
    with monkeypatch.context() as patch:
        patch.setattr(quota, "terminal_regional_hold_amount", failing)
        for visit in range(3 if transient else 1):
            result = store.reconcile_regional_quota_leases(now=later)
            assert result["errors"] == 1 and result["closed"] == 0
            assert result["reconciled"] == int(transient)
            local = ledger.get(lease.lease_id, region=lease.region)
            assert next(h for h in local.holds if h.hold_id == "a-failing").state == HoldState.RESERVED
            assert local.reserved_microdollars == (10_000 if transient else 20_000)
            assert local.state.value == ("draining" if transient else "active")
            current = store._read_entity(
                "regional_quota_lease", lease.entity_id, quota.GlobalRegionalQuotaLease,
            )
            assert current.reconciled_spent_microdollars == (7500 if transient else 0)
            cursor = store._read_entity(
                "regional_quota_reconciler_cursor", "singleton", quota.RegionalReconcileCursor,
            )
            assert cursor.holds[lease.entity_id] == (
                auth.id if transient and visit == 0 else "a-failing"
            )
    assert attempted == (["a-failing", auth.id, "a-failing", "a-failing"] if transient else ["a-failing"])
    assert lease.lease_id in caplog.text
    if transient:
        assert "hold_id=a-failing" in caplog.text
    else:
        assert "hold lookup programming error" in caplog.text
        assert "regional quota reconciliation failed" in caplog.text
    # A later successful lookup cancels the orphan and closes without reimporting spend.
    result = store.reconcile_regional_quota_leases(now=later)
    assert result["errors"] == 0 and result["closed"] == result["reconciled"] == 1
    local = ledger.get(lease.lease_id, region=lease.region)
    assert local.state.value == "closed" and local.reserved_microdollars == 0
    assert local.spent_microdollars == 7500
    assert sum(row["total_usage"] for row in db.typed["tr_credit_balance"].values()) == 7500
    assert sum(row["reserved"] for row in db.typed["tr_credit_balance"].values()) == 0


def test_hold_cursor_advances_past_slow_guarded_holds_and_preserves_drain_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from trusted_router import storage_gcp

    store, db, ledger, auth = _authorized()
    lease = store._list_entities("regional_quota_lease", cls=quota.GlobalRegionalQuotaLease)[0]
    # The fourth hold is already reaped in Spanner but not yet refunded locally.
    with monkeypatch.context() as patch:
        patch.setattr(type(store), "_finalize_regional_quota_hold", lambda *_a, **_kw: None)
        assert store.typed_finalize_gateway_authorization_result(
            auth.id, success=False, actual_microdollars=0, selected_usage_type=UsageType.CREDITS,
        ).finalized
    for hold_id in ("a", "b", "c"):
        ledger.reserve(
            lease.lease_id, region=lease.region, hold_id=hold_id, fingerprint=hold_id,
            amount_microdollars=10_000, fencing_token=lease.fencing_token,
            key_hash=auth.key_hash, key_shard=0,
            hold_expires_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        store.settle_outbox.enqueue(SettleOutboxRow(
            authorization_id=hold_id, reservation_id="unknown", intent_kind="settle",
            settle_origin="typed", actual_cost_micro=7500, settle_body="{}",
        ))
        db.settle_outbox[(hold_id, "settle")]["status"] = "dead"
    other = _pending(store, "zz-other", "region", 0)
    other_record = store._read_entity(
        "regional_quota_lease", other.lease_entity_id, quota.GlobalRegionalQuotaLease,
    )
    store._write_entity(
        "regional_quota_lease", other.lease_entity_id,
        replace(other_record, expires_at=(datetime.now(UTC) + timedelta(days=1)).isoformat()),
    )
    attempted = []
    elapsed = [0.0]
    original = quota.terminal_regional_hold_amount

    def slow(*args: Any, **kwargs: Any) -> Any:
        attempted.append(args[2])
        elapsed[0] += 16
        return original(*args, **kwargs)

    monkeypatch.setattr(storage_gcp, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(quota, "terminal_regional_hold_amount", slow)
    later = datetime.now(UTC) + timedelta(hours=3)
    for _ in range(4):
        before = len(attempted)
        result = store.reconcile_regional_quota_leases(now=later)
        assert result["errors"] == 0 and result["processed"] == 2
        assert result["reconciled"] >= 1  # every visit imports/drains, even unresolved
        assert len(attempted) - before == 1
        cursor = store._read_entity(
            "regional_quota_reconciler_cursor", "singleton", quota.RegionalReconcileCursor,
        )
        assert cursor.holds[lease.entity_id] == attempted[-1]
        assert cursor.leases[f"{other.workspace_id}#{other.region}"] == other.entity_id
    assert attempted == ["a", "b", "c", auth.id]
    local = ledger.get(lease.lease_id, region=lease.region)
    assert local.state.value == "draining" and local.reserved_microdollars == 30_000
    assert next(h for h in local.holds if h.hold_id == auth.id).state == HoldState.REFUNDED
    assert store.reconcile_regional_quota_leases(now=later)["reconciled"] >= 1
    assert attempted[-1] == "a"  # wraps over only unresolved holds


@pytest.mark.parametrize("global_fallback", [False, True])
def test_committed_cancellation_survives_failed_refund_until_next_pass(
    monkeypatch: pytest.MonkeyPatch, global_fallback: bool,
) -> None:
    store, db, ledger, auth = _orphan(monkeypatch)
    later = datetime.now(UTC) + timedelta(days=1)

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("regional storage unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(type(ledger), "refund", unavailable)
        first = store.reconcile_regional_quota_leases(now=later)
    assert first["errors"] == 1 and first["closed"] == 0
    tombstone = db.rows[("regional_quota_hold_cancellation", auth.id)].body
    assert ledger.get(auth.regional_lease_id, region=auth.region).reserved_microdollars == 10_000
    if global_fallback:
        assert _record(store, auth)["outcome"] == "cancelled"
        outcome, typed = store.authorize_gateway_typed(
            authorization_id=auth.id, workspace_id=auth.workspace_id, key_hash=auth.key_hash,
            estimate=10_000, has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS,
            model_id=auth.model_id, provider=auth.provider, requested_model_id=None,
            candidate_model_ids=auth.candidate_model_ids, region=auth.region,
            endpoint_id=auth.endpoint_id, candidate_endpoint_ids=auth.candidate_endpoint_ids,
            idempotency_key=None, idempotency_fingerprint=None,
        )
        assert outcome == "accepted" and typed is not None
        assert typed.settlement != "regional_lease"
    second = store.reconcile_regional_quota_leases(now=later + timedelta(days=30))
    assert second["closed"] == 1 and second["errors"] == 0
    assert db.rows[("regional_quota_hold_cancellation", auth.id)].body == tombstone
    local = ledger.get(auth.regional_lease_id, region=auth.region)
    assert local.state.value == "closed" and local.reserved_microdollars == 0
    assert sum(row["reserved"] for row in db.typed["tr_credit_balance"].values()) == (
        10_000 if global_fallback else 0
    )


def test_slow_leases_leave_unvisited_lease_first_in_next_invocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from trusted_router import storage_gcp

    store, db, _ = make_fake_store(request_record_write_mode="typed")
    ledger = InMemoryRegionalQuotaLedger()
    store._regional_quota_ledger = ledger
    auths = []
    for i in range(3):
        ws = store.create_workspace("owner", f"slow-{i}", trial_credit_microdollars=200_000_000)
        _raw, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
        outcome, auth = store.authorize_gateway_regional(
            authorization_id=f"slow-{i}", **regional_args(ws.id, key),
        )
        assert outcome == "accepted" and auth is not None
        auths.append(auth)
    auths.sort(key=lambda auth: auth.workspace_id)
    for auth in auths[:2]:
        store.settle_outbox.enqueue(SettleOutboxRow(
            authorization_id=auth.id, reservation_id=auth.credit_reservation_id,
            intent_kind="settle", settle_origin="typed", actual_cost_micro=7500,
            settle_body="{}",
        ))
        db.settle_outbox[(auth.id, "settle")]["status"] = "dead"
    terminal = auths[2]
    with monkeypatch.context() as patch:
        patch.setattr(type(store), "_finalize_regional_quota_hold", lambda *_a, **_kw: None)
        assert store.typed_finalize_gateway_authorization_result(
            terminal.id, success=False, actual_microdollars=0,
            selected_usage_type=UsageType.CREDITS,
        ).finalized
    elapsed = [0.0]
    attempted = []
    original = quota.terminal_regional_hold_amount

    def slow(*args: Any, **kwargs: Any) -> Any:
        attempted.append(args[2])
        elapsed[0] += 16
        return original(*args, **kwargs)

    monkeypatch.setattr(storage_gcp, "time", SimpleNamespace(monotonic=lambda: elapsed[0]))
    monkeypatch.setattr(quota, "terminal_regional_hold_amount", slow)
    later = datetime.now(UTC) + timedelta(hours=3)
    first = store.reconcile_regional_quota_leases(now=later)
    assert first["errors"] == 0 and first["reconciled"] == first["processed"] == 2
    assert first["remaining"] == 1 and first["closed"] == 0
    assert attempted == [auth.id for auth in auths[:2]]
    second = store.reconcile_regional_quota_leases(now=later)
    assert second["errors"] == 0 and second["closed"] == 1
    assert attempted[2] == terminal.id
    assert ledger.get(terminal.regional_lease_id, region=terminal.region).reserved_microdollars == 0
