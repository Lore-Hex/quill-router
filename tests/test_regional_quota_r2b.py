"""R2b money-path regressions: allocation, liquidity, and overlapping generations."""
from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

import pytest

from tests.fakes.spanner import make_fake_store
from tests.fakes.spanner_order import StatementCalls, record_statements
from tests.test_regional_accounting_v2 import _totals
from tests.test_regional_quota_ledger import _FakeBigtableTable
from tests.test_trust_eligibility_pr2 import arm_store, regional_args, workspace_state
from trusted_router import storage_gcp_regional_quota as quota
from trusted_router.config import Settings
from trusted_router.regional_quota_ledger import (
    BigtableRegionalQuotaLedger,
    InMemoryRegionalQuotaLedger,
)
from trusted_router.services.regional_quota_leases import LeaseUnavailableError
from trusted_router.storage_gcp_counter_reconcile import audit_typed_invariants
from trusted_router.types import UsageType


def setup(balance: int = 100_000_000) -> tuple[Any, Any, Any, dict[str, Any]]:
    store, db, _ = make_fake_store(request_record_write_mode="typed")
    arm_store(store, db)
    ws = store.create_workspace("owner", "r2b", trial_credit_microdollars=0)
    workspace_state(db, 1, ws.id)["total_credits"] = balance
    _, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
    store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
    return store, db, key, regional_args(ws.id, key)


def authorize(store: Any, args: dict[str, Any], name: str, **overrides: Any) -> Any:
    result, auth = store.authorize_gateway_regional(
        authorization_id=name, **{**args, "idempotency_key": name, **overrides},
    )
    assert result == "accepted" and auth is not None
    return auth


def global_lease(store: Any, auth: Any) -> Any:
    return quota.get_global_regional_quota_lease(
        store, workspace_id=auth.workspace_id, region=auth.region, lease_id=auth.regional_lease_id,
    )


def escrow(db: Any) -> int:
    return sum(row["reserved"] for row in db.typed["tr_credit_balance"].values())


def grant(store: Any, workspace: str, region: str, **overrides: Any) -> Any:
    return quota.grant_regional_quota_lease(store, **{
        "workspace_id": workspace, "region": region, "requested_microdollars": 5_000_000,
        "per_lease_cap_microdollars": 5_000_000, "max_available_basis_points": 10_000,
        "minimum_grant_microdollars": 500_000, "ttl_seconds": 300, "adaptive": True,
        **overrides,
    })


def test_concurrent_four_regions_divide_one_pool_by_traffic_not_region() -> None:
    store, db, key, _ = setup()
    db._ready_barrier = threading.Barrier(4)
    with ThreadPoolExecutor(max_workers=4) as executor:
        leases = list(executor.map(lambda n: grant(store, key.workspace_id, f"region-{n}"), range(4)))
    assert all(lease is not None for lease in leases)
    # A per-region split funds three regions and starves the fourth. This is
    # deliberately stronger than just the unchanged aggregate pool invariant.
    assert sorted(lease.granted_microdollars for lease in leases) == [625_000, 833_333, 1_250_000, 2_000_000]
    assert escrow(db) == 4_708_333 < 5_000_000
    assert db.aborts >= 3


@pytest.mark.parametrize("floor_bp", [5000, 8000, 10000])
def test_concurrent_grants_keep_aggregate_liquidity_floor(floor_bp: int) -> None:
    store, db, key, _ = setup(7_090_000)
    store.trust_settings.regional_quota_global_floor_basis_points = floor_bp
    db._ready_barrier = threading.Barrier(4)
    with ThreadPoolExecutor(max_workers=4) as executor:
        leases = list(executor.map(lambda n: grant(
            store, key.workspace_id, f"region-{n}", adaptive=False,
            requested_microdollars=2_000_000, minimum_grant_microdollars=1,
        ), range(4)))
    expected = 7_090_000 * (10_000 - floor_bp) // 10_000
    assert sum(lease.granted_microdollars for lease in leases if lease is not None) == expected
    assert escrow(db) == expected
    assert 7_090_000 - escrow(db) >= 7_090_000 * floor_bp // 10_000


def test_low_balance_global_authorization_and_charge_survive_parked_escrow() -> None:
    store, db, key, args = setup(7_090_000)
    first = grant(store, key.workspace_id, "idle-region", adaptive=False, minimum_grant_microdollars=1)
    assert first.granted_microdollars == 3_545_000
    evidence: dict[str, Any] = {}
    assert grant(store, key.workspace_id, "another-idle-region", adaptive=False,
                 minimum_grant_microdollars=1, observation=evidence) is None
    assert evidence == {"regional_unavailable_reason": "pool_floor"}
    typed = {k: v for k, v in args.items() if not k.startswith("lease_") and k != "key_usage_shards"}
    result, auth = store.authorize_gateway_typed(
        authorization_id="global-headroom", **{**typed, "estimate": 3_000_000},
        has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS, skip_key_limit=True,
    )
    assert result == "accepted" and auth is not None
    assert store.typed_finalize_gateway_authorization_result(
        auth.id, success=True, actual_microdollars=10, selected_usage_type=UsageType.CREDITS,
    ).finalized
    assert _totals(db, key.workspace_id, key.hash) == (10, 10, 10, 10, 10)


def assert_no_shared_writes(statements: StatementCalls) -> None:
    for _, sql in statements:
        for table in ("tr_credit_balance", "tr_key_limit", "tr_entities"):
            assert not sql.startswith((
                f"update {table}", f"insert into {table}", f"delete from {table}",  # noqa: S608
            )), sql
            assert not (sql.startswith("mutation:") and sql.split()[-1] == table), sql


@pytest.mark.parametrize("buffered", [False, True])
def test_sibling_shared_write_negative_control(
    monkeypatch: pytest.MonkeyPatch, buffered: bool,
) -> None:
    original_record = quota.record_regional_gateway_authorization

    def inject_shared_write(store: Any, **kwargs: Any) -> Any:
        auth = kwargs["authorization"]
        lease = global_lease(store, auth)

        def write(tx: Any) -> None:
            if buffered:
                tx.insert_or_update(
                    table="tr_entities", columns=("kind", "id", "body"),
                    values=[("regional_quota_lease", lease.entity_id, quota._regional_json_body(lease))],
                )
            else:
                quota._upsert_entity_dml(
                    tx, store._param_types, "regional_quota_lease", lease.entity_id, lease,
                )
        store._run_in_transaction(write)
        return original_record(store, **kwargs)

    monkeypatch.setattr(quota, "record_regional_gateway_authorization", inject_shared_write)
    # Run the actual sibling scenario with a real canonical-row write. A
    # broken guard must fail this negative control, not silently pass it.
    with pytest.raises(AssertionError, match="tr_entities"):
        test_unfunded_hash_shard_uses_funded_sibling_without_shared_writes(monkeypatch)


def test_unfunded_hash_shard_uses_funded_sibling_without_shared_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, key, args = setup()
    first = authorize(store, args, "funded")
    funded = global_lease(store, first)
    fingerprint = next(str(i) for i in range(100) if
                       int.from_bytes(hashlib.sha256(str(i).encode()).digest()[:4], "big") % 16
                       == (funded.quota_shard - 1) % 16)
    # Simulate a different process: fallback must work without its local cache.
    store._regional_quota_lease_cache.clear()
    statements = record_statements(monkeypatch)
    evidence: dict[str, Any] = {}
    before = escrow(db)
    second = authorize(store, args, "sibling", idempotency_fingerprint=fingerprint, observation=evidence)
    assert second.regional_lease_id == first.regional_lease_id
    assert evidence["regional_selected_shard"] == funded.quota_shard
    assert evidence["regional_sibling_served"] is True
    assert escrow(db) == before
    assert_no_shared_writes(statements)
    assert _totals(db, key.workspace_id, key.hash) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("trigger", ["exhaustion", "expiry"])
def test_handoff_serves_successor_while_old_holds_settle_at_global_oracle_charge(
    monkeypatch: pytest.MonkeyPatch, trigger: str,
) -> None:
    store, db, key, args = setup()
    first = authorize(store, args, "old")
    old = global_lease(store, first)
    ledger = store._regional_quota_ledger
    if trigger == "exhaustion":
        # Each request must succeed before any old hold is settled/reconciled.
        authorize(store, args, "old-2")
        authorize(store, args, "old-3")
    else:
        # Deterministic clock, just inside the proactive expiry window.
        class Clock(datetime):
            @classmethod
            def now(cls, tz: Any = None) -> datetime:
                return old.expires_datetime - timedelta(seconds=2)
        monkeypatch.setattr("trusted_router.storage_gcp.dt.datetime", Clock)
    second = authorize(store, args, "successor")
    successor = global_lease(store, second)
    assert successor.lease_id != old.lease_id
    assert successor.fencing_token == old.fencing_token + 1
    if trigger == "exhaustion":
        assert successor.granted_microdollars > old.granted_microdollars  # measured growth
    retired = global_lease(store, first)
    assert retired.state == "retiring"
    assert ("regional_quota_lease_retired", old.entity_id) in db.rows
    assert ("regional_quota_lease_workspace_open", old.entity_id) in db.rows
    assert ("regional_quota_lease_workspace_open", successor.entity_id) in db.rows
    fence = store._read_entity("regional_quota_fence",
                              quota._fence_entity_id(key.workspace_id, old.region, old.quota_shard),
                              quota.RegionalQuotaFence)
    assert fence.active_lease_id == successor.lease_id
    assert escrow(db) == old.granted_microdollars + successor.granted_microdollars <= 5_000_000
    assert audit_typed_invariants(store).clean
    with pytest.raises(LeaseUnavailableError):
        ledger.reserve(old.lease_id, region=old.region, hold_id="late", fingerprint="late",
                       amount_microdollars=1, fencing_token=old.fencing_token,
                       key_hash=key.hash, key_shard=0)

    typed = {k: v for k, v in args.items() if not k.startswith("lease_") and k != "key_usage_shards"}
    result, oracle = store.authorize_gateway_typed(
        authorization_id="oracle", **{**typed, "idempotency_key": "oracle"},
        has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS, skip_key_limit=True,
    )
    assert result == "accepted" and oracle is not None
    # Same frozen request charge through the global path is the money oracle.
    before = _totals(db, key.workspace_id, key.hash)
    assert store.typed_finalize_gateway_authorization_result(
        oracle.id, success=True, actual_microdollars=7_500, selected_usage_type=UsageType.CREDITS,
    ).finalized
    oracle_delta = tuple(a - b for a, b in zip(_totals(db, key.workspace_id, key.hash), before, strict=True))
    for auth in (first, second):
        before = _totals(db, key.workspace_id, key.hash)
        assert store.typed_finalize_gateway_authorization_result(
            auth.id, success=True, actual_microdollars=7_500, selected_usage_type=UsageType.CREDITS,
        ).finalized
        assert not store.typed_finalize_gateway_authorization_result(
            auth.id, success=True, actual_microdollars=7_500, selected_usage_type=UsageType.CREDITS,
        ).finalized
        store.reconcile_regional_quota_leases()
        assert tuple(a - b for a, b in zip(_totals(db, key.workspace_id, key.hash), before, strict=True)) == oracle_delta
    # Close the retired generation while successor is still current. Closing
    # must not clear or overwrite the successor fence (mutation p5).
    if trigger == "exhaustion":
        for name in ("old-2", "old-3"):
            assert store.typed_finalize_gateway_authorization_result(
                name, success=False, actual_microdollars=0, selected_usage_type=UsageType.CREDITS,
            ).finalized
    result = store.reconcile_regional_quota_leases()
    assert result["errors"] == 0
    assert global_lease(store, first).state == "closed"
    assert audit_typed_invariants(store).clean
    assert ("regional_quota_lease_retired", old.entity_id) not in db.rows
    assert quota.active_regional_quota_leases(store, workspace_id=key.workspace_id,
                                             region=old.region, quota_shard=old.quota_shard)[0].lease_id == successor.lease_id


def test_successor_checks_pool_including_retired_generation() -> None:
    store, db, key, _ = setup()
    old = grant(store, key.workspace_id, "region", adaptive=False,
                requested_microdollars=4_000_000, minimum_grant_microdollars=1)
    old = quota.activate_regional_quota_lease(store, old)
    local = quota.regional_lease_from_global(old).begin_drain(fencing_token=old.fencing_token)
    quota.retire_regional_quota_lease(store, old, local)
    evidence: dict[str, Any] = {}
    assert grant(store, key.workspace_id, "region", adaptive=False,
                 minimum_grant_microdollars=2_000_000, observation=evidence) is None
    assert evidence == {"regional_unavailable_reason": "pool_cap"}
    successor = grant(store, key.workspace_id, "region", adaptive=False, minimum_grant_microdollars=1)
    assert successor.granted_microdollars == 1_000_000
    assert escrow(db) == 5_000_000
    assert quota.reconcile_regional_quota_lease(store, old, local, close=True).closed
    assert escrow(db) == successor.granted_microdollars


@pytest.mark.parametrize("crash_at", ["after_drain", "after_retire", "after_grant", "after_initialize"])
def test_crash_recovery_preserves_generations_and_pool(
    monkeypatch: pytest.MonkeyPatch, crash_at: str,
) -> None:
    store, db, key, args = setup()
    old_auth = authorize(store, args, "first")
    authorize(store, args, "second")
    authorize(store, args, "third")
    old = global_lease(store, old_auth)
    retire = quota.retire_regional_quota_lease
    mint = quota.grant_regional_quota_lease

    def crash_retire(*a: Any, **kw: Any) -> None:
        if crash_at == "after_retire":
            retire(*a, **kw)
        raise SystemExit("crash")

    def crash_grant(*a: Any, **kw: Any) -> Any:
        mint(*a, **kw)
        raise SystemExit("crash")

    with monkeypatch.context() as patch:
        if crash_at == "after_initialize":
            patch.setattr(quota, "activate_regional_quota_lease", crash_retire)
        else:
            patch.setattr(quota, "grant_regional_quota_lease" if crash_at == "after_grant"
                      else "retire_regional_quota_lease",
                      crash_grant if crash_at == "after_grant" else crash_retire)
        with pytest.raises(SystemExit):
            authorize(store, args, "crash")
    store._regional_quota_lease_cache.clear()
    reserved_before_retry = escrow(db)
    successor = authorize(store, args, "retry")
    assert successor.regional_lease_id != old.lease_id
    assert global_lease(store, old_auth).state == "retiring"
    if crash_at in {"after_grant", "after_initialize"}:
        # Resume the existing pending generation without reserving again.
        assert escrow(db) == reserved_before_retry
        owned = store._run_in_transaction(lambda tx: quota._owned_regional_leases(tx, store._param_types, key.workspace_id))
        assert len(owned) == 2
    assert escrow(db) <= 5_000_000
    assert ("regional_quota_lease_workspace_open", old.entity_id) in db.rows
    assert store._regional_quota_ledger.get(old.lease_id, region=old.region).reserved_microdollars == 30_000


@pytest.mark.parametrize("floor", [0, 10001])
def test_floor_configuration_cannot_disable_liquidity(floor: int) -> None:
    with pytest.raises(ValueError, match="GLOBAL_FLOOR"):
        Settings(environment="test", regional_quota_global_floor_basis_points=floor)


def test_router_uses_workspace_split_for_four_busy_regions() -> None:
    store, db, _key, args = setup()
    amounts = []
    for n in range(4):
        auth = authorize(store, args, f"region-{n}", region=f"region-{n}", estimate=500_000)
        amounts.append(global_lease(store, auth).granted_microdollars)
    assert amounts == [2_000_000, 1_250_000, 833_333, 625_000]
    assert escrow(db) == sum(amounts)


def test_stale_cached_generation_refreshes_to_live_successor() -> None:
    store, db, _key, args = setup()
    first = authorize(store, args, "first")
    old = global_lease(store, first)
    authorize(store, args, "second")
    authorize(store, args, "third")
    successor = authorize(store, args, "successor")
    for name in ("first", "second", "third"):
        assert store.typed_finalize_gateway_authorization_result(
            name, success=False, actual_microdollars=0, selected_usage_type=UsageType.CREDITS,
        ).finalized
    assert store.reconcile_regional_quota_leases()["closed"] == 1
    store._regional_quota_lease_cache[(old.workspace_id, old.region, old.quota_shard)] = old
    before = escrow(db)
    assert authorize(store, args, "stale-cache").regional_lease_id == successor.regional_lease_id
    assert escrow(db) == before


def test_floor_accounts_for_global_holds_and_debt_on_other_credit_shards() -> None:
    store, db, key, _ = setup(7_090_000)
    # Debt on a different shard must reduce the workspace denominator rather
    # than being hidden by clamping each shard separately to zero.
    db.typed["tr_credit_balance"][(key.workspace_id, 1)]["total_usage"] = 1_000_000
    db.typed["tr_credit_balance"][(key.workspace_id, 0)]["reserved"] = 100_000
    lease = grant(store, key.workspace_id, "region", adaptive=False, minimum_grant_microdollars=1)
    assert lease.granted_microdollars == (7_090_000 - 1_000_000 - 100_000) // 2


@pytest.mark.parametrize("outcome", ["billing_paused", "replay"])
def test_sibling_attempt_is_not_reported_as_served_when_record_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch, outcome: str,
) -> None:
    store, _db, _key, args = setup()
    first = authorize(store, args, "funded")
    funded = global_lease(store, first)
    fingerprint = next(str(i) for i in range(100) if
                       int.from_bytes(hashlib.sha256(str(i).encode()).digest()[:4], "big") % 16
                       == (funded.quota_shard - 1) % 16)
    monkeypatch.setattr(quota, "record_regional_gateway_authorization",
                        lambda *a, **kw: {"outcome": outcome, "authorization_id": first.id})
    evidence: dict[str, Any] = {}
    result, _ = store.authorize_gateway_regional(authorization_id="attempt", **{
        **args, "idempotency_key": "attempt", "idempotency_fingerprint": fingerprint,
        "observation": evidence,
    })
    assert result == outcome
    assert evidence["regional_selected_shard"] == funded.quota_shard
    assert evidence["regional_sibling_served"] is False


def test_four_busy_regions_on_measured_low_balance_use_trust_pool_and_keep_headroom() -> None:
    store, db, key, args = setup(7_090_000)
    leases = []
    for n in range(4):
        auth = authorize(store, args, f"busy-{n}", region=f"region-{n}", estimate=100_000)
        leases.append(global_lease(store, auth))
    # The existing 10% PER-GRANT ceiling must not accidentally become a
    # second, tiny workspace pool. All four regions can carry these estimates.
    assert [lease.granted_microdollars for lease in leases] == [400_000] * 4
    assert escrow(db) == 1_600_000
    assert 7_090_000 - escrow(db) >= 7_090_000 // 2
    assert audit_typed_invariants(store).clean
    assert _totals(db, key.workspace_id, key.hash) == (0, 0, 0, 0, 0)


@pytest.mark.parametrize("successor_available", [True, False])
def test_quarantined_retired_cache_entry_allows_successor_or_global_fallback(
    monkeypatch: pytest.MonkeyPatch, successor_available: bool,
) -> None:
    store, db, key, args = setup()
    first = authorize(store, args, "first")
    second = authorize(store, args, "second")
    old = global_lease(store, first)
    reserved = threading.Event()
    resume = threading.Event()
    record = quota.record_regional_gateway_authorization

    def delay_record(*a: Any, **kw: Any) -> Any:
        if kw["authorization"].id == "delayed":
            reserved.set()
            assert resume.wait(10)
        return record(*a, **kw)

    monkeypatch.setattr(quota, "record_regional_gateway_authorization", delay_record)
    with ThreadPoolExecutor(max_workers=1) as executor:
        delayed = executor.submit(store.authorize_gateway_regional,
                                  authorization_id="delayed", **{**args, "idempotency_key": "delayed"})
        try:
            assert reserved.wait(10)
            successor_auth = authorize(store, args, "successor")
            successor = global_lease(store, successor_auth)
            assert successor.lease_id != old.lease_id
            assert global_lease(store, first).state == "retiring"
            for (ws, _), row in db.typed["tr_credit_balance"].items():
                if ws == key.workspace_id:
                    row["billing_pause_causes"] = ["abuse"]
        finally:
            resume.set()
        assert delayed.result(timeout=10) == ("billing_paused", None)
    assert global_lease(store, first).state == "quarantined"
    for (ws, _), row in db.typed["tr_credit_balance"].items():
        if ws == key.workspace_id:
            row["billing_pause_causes"] = []
    if not successor_available:
        quota.quarantine_regional_quota_lease(store, successor, reason="unavailable successor")
        store._regional_quota_lease_cache.clear()
    cache_key = (old.workspace_id, old.region, old.quota_shard)
    store._regional_quota_lease_cache[cache_key] = old
    before = escrow(db)
    fence_key = ("regional_quota_fence", quota._fence_entity_id(*cache_key))
    fence_before = db.rows[fence_key]
    local = store._regional_quota_ledger.get(old.lease_id, region=old.region)
    assert quota.retire_regional_quota_lease(store, old, local) is False
    result, auth = store.authorize_gateway_regional(
        authorization_id="after-recovery", **{**args, "idempotency_key": "after-recovery"},
    )
    assert escrow(db) == before
    assert db.rows[fence_key] == fence_before
    assert store._regional_quota_lease_cache.get(cache_key) != old
    if successor_available:
        assert result == "accepted" and auth.regional_lease_id == successor.lease_id
    else:
        assert (result, auth) == ("unavailable", None)
        typed = {k: v for k, v in args.items() if not k.startswith("lease_") and k != "key_usage_shards"}
        result, auth = store.authorize_gateway_typed(
            authorization_id="global-fallback", **{**typed, "idempotency_key": "global-fallback"},
            has_credit_candidate=True, reservation_usage_type=UsageType.CREDITS, skip_key_limit=True,
        )
        assert result == "accepted" and auth is not None
    for accepted in (first, second, successor_auth, auth):
        assert store.typed_finalize_gateway_authorization_result(
            accepted.id, success=True, actual_microdollars=100, selected_usage_type=UsageType.CREDITS,
        ).finalized
    assert store.reconcile_regional_quota_leases()["errors"] == 0
    assert global_lease(store, first).state == "closed"
    assert _totals(db, key.workspace_id, key.hash) == (400, 400, 400, 400, 400)
    assert audit_typed_invariants(store).clean


@pytest.mark.parametrize("backend", ["memory", "bigtable"])
@pytest.mark.parametrize("progress", ["active", "retiring", "closed"])
def test_live_issuer_resumes_after_pending_recovery_without_quarantining_progress(
    monkeypatch: pytest.MonkeyPatch, backend: str, progress: str,
) -> None:
    store, db, key, args = setup()
    if backend == "bigtable":
        store._regional_quota_ledger = BigtableRegionalQuotaLedger({args["region"]: _FakeBigtableTable()})
    committed = threading.Event()
    resume = threading.Event()
    mint = quota.grant_regional_quota_lease
    issued = []

    def suspend_issuer(*a: Any, **kw: Any) -> Any:
        lease = mint(*a, **kw)
        if not committed.is_set():
            issued.append(lease)
            committed.set()
            assert resume.wait(10)
        return lease

    monkeypatch.setattr(quota, "grant_regional_quota_lease", suspend_issuer)
    with ThreadPoolExecutor(max_workers=1) as executor:
        issuer = executor.submit(authorize, store, args, "issuer")
        try:
            assert committed.wait(10)
            recovered = authorize(store, args, "recovery")
            assert recovered.regional_lease_id == issued[0].lease_id
            old_holds = [recovered]
            target = recovered
            if progress != "active":
                old_holds.extend(authorize(store, args, name) for name in ("fill-2", "fill-3"))
                target = authorize(store, args, "successor")
            if progress == "closed":
                for auth in old_holds:
                    assert store.typed_finalize_gateway_authorization_result(
                        auth.id, success=True, actual_microdollars=100,
                        selected_usage_type=UsageType.CREDITS,
                    ).finalized
                assert store.reconcile_regional_quota_leases()["errors"] == 0
            assert global_lease(store, recovered).state == progress
            local_before = store._regional_quota_ledger.get(issued[0].lease_id, region=args["region"])
            escrow_before = escrow(db)
        finally:
            resume.set()
        resumed = issuer.result(timeout=10)
    assert resumed.regional_lease_id == target.regional_lease_id
    assert global_lease(store, recovered).state == progress
    assert escrow(db) == escrow_before
    local_after = store._regional_quota_ledger.get(issued[0].lease_id, region=args["region"])
    assert local_after.holds[:len(local_before.holds)] == local_before.holds
    assert local_after.state == local_before.state
    for auth in [*old_holds, *([] if target == recovered else [target]), resumed]:
        store.typed_finalize_gateway_authorization_result(
            auth.id, success=True, actual_microdollars=100, selected_usage_type=UsageType.CREDITS,
        )
    assert store.reconcile_regional_quota_leases()["errors"] == 0
    total = 100 * (len(old_holds) + (target != recovered) + 1)
    assert _totals(db, key.workspace_id, key.hash) == (total,) * 5
    assert audit_typed_invariants(store).clean
