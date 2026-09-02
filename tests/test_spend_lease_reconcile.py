from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tests.fakes.spanner import FakeSpannerDatabase, make_fake_store
from tests.fakes.spend_lease_bigtable import FakeBigtableTable
from trusted_router.spend_lease_ledger import BigtableSpendLeaseLedger
from trusted_router.spend_lease_state import (
    BindingState,
    SpendLease,
    SpendLeaseConflictError,
    SpendLeaseState,
)
from trusted_router.storage_gcp_spend_lease import (
    CandidateIdentity,
    insert_candidate,
    take_recovery_ownership,
    upgrade_candidate_to_open,
)
from trusted_router.storage_gcp_spend_lease_authorize import FENCE_KIND, SPEND_LEASE_KIND
from trusted_router.storage_gcp_spend_lease_reconcile import (
    acquire_spend_lease_reconciler_lock,
    reconcile_spend_leases,
    release_spend_lease_reconciler_lock,
    requeue_dead_spend_leases,
)

NOW = datetime(2026, 9, 2, 12, tzinfo=UTC)
REGION = "us-central1"


def _store() -> tuple[Any, FakeSpannerDatabase, BigtableSpendLeaseLedger]:
    store, database, _ = make_fake_store(request_record_write_mode="typed")
    database.now = NOW
    table = FakeBigtableTable()
    ledger = BigtableSpendLeaseLedger({REGION: table})
    store._spend_lease_ledger = ledger
    return store, database, ledger


def _identity(
    *,
    lease_id: str = "lease-1",
    expires_at: datetime | None = None,
) -> CandidateIdentity:
    return CandidateIdentity(
        lease_id=lease_id,
        gen=3,
        key_hash="k" * 64,
        boot_kid="boot-1",
        cap_micro=10_000,
        skew_seconds=10,
        workspace_id="workspace-1",
        region=REGION,
        creating_authorization_id="creator-1",
        idempotency_scope="workspace-1:scope-1",
        expires_at=expires_at or NOW - timedelta(minutes=2),
    )


def _insert_candidate(store: Any, identity: CandidateIdentity) -> None:
    store._database.run_in_transaction(
        lambda transaction: insert_candidate(
            transaction,
            store._param_types,
            identity,
            created_at=NOW - timedelta(hours=2),
        )
    )


def _local_candidate(identity: CandidateIdentity, *, state: SpendLeaseState = SpendLeaseState.ACTIVE) -> SpendLease:
    return SpendLease(
        lease_id=identity.lease_id,
        gen=identity.gen,
        key_hash=identity.key_hash,
        boot_kid=identity.boot_kid,
        workspace_id=identity.workspace_id,
        creating_authorization_id=identity.creating_authorization_id,
        cap_micro=identity.cap_micro,
        expires_at=identity.expires_at,
        skew=timedelta(seconds=identity.skew_seconds),
        version=0,
        state=state,
    )


def _allocate(ledger: BigtableSpendLeaseLedger, identity: CandidateIdentity) -> None:
    ledger.allocate(
        None,
        identity.lease_id,
        region=identity.region,
        idempotency_scope=identity.idempotency_scope,
        provisional_authorization_id=identity.creating_authorization_id,
        request_fingerprint="fingerprint-1",
        allocated_micro=500,
        abandon_after=identity.expires_at + timedelta(seconds=identity.skew_seconds),
        now=identity.expires_at - timedelta(seconds=1),
    )


def _open_candidate(store: Any, identity: CandidateIdentity) -> None:
    store._database.run_in_transaction(
        lambda transaction: upgrade_candidate_to_open(
            transaction,
            store._param_types,
            identity.lease_id,
            identity.creating_authorization_id,
            identity.expires_at,
            identity.skew_seconds,
        )
    )


def _global_body(identity: CandidateIdentity, *, state: str = "DRAINING", slot: bool = False) -> dict[str, Any]:
    return {
        "state": state,
        "lease_id": identity.lease_id,
        "gen": identity.gen,
        "key_hash": identity.key_hash,
        "boot_kid": identity.boot_kid,
        "workspace_id": identity.workspace_id,
        "region": identity.region,
        "cap_micro": identity.cap_micro,
        "expires_at": identity.expires_at.isoformat(),
        "skew_seconds": identity.skew_seconds,
        "credit_shard": 0,
        "frozen_local_version": None,
        "holds_predecessor_slot": slot,
        "closing_at": None,
        "last_error": None,
    }


def _seed_global(store: Any, identity: CandidateIdentity, *, state: str = "DRAINING", slot: bool = False, count: int = 1) -> None:
    store._write_entity(SPEND_LEASE_KIND, identity.lease_id, _global_body(identity, state=state, slot=slot))
    store._write_entity(
        FENCE_KIND,
        store._spend_lease_pair_id(identity.key_hash, identity.boot_kid),
        {
            "lease_id": "successor" if slot else identity.lease_id,
            "gen": identity.gen + int(slot),
            "open_predecessor_count": count,
            "lease_status": "open",
        },
    )


def _seed_credit(database: FakeSpannerDatabase, identity: CandidateIdentity) -> None:
    database.typed.setdefault("tr_credit_balance", {})[(identity.workspace_id, 0)] = {
        "workspace_id": identity.workspace_id,
        "shard": 0,
        "total_credits": 100_000,
        "total_usage": 0,
        "reserved": identity.cap_micro,
    }


def test_candidate_recovery_compensates_then_tombstones_then_completes() -> None:
    store, database, ledger = _store()
    identity = _identity()
    _insert_candidate(store, identity)
    ledger.initialize(_local_candidate(identity), region=REGION)
    _allocate(ledger, identity)

    result = reconcile_spend_leases(store, now=NOW)

    row = database.spend_lease_open[identity.lease_id]
    local = ledger.get(identity.lease_id, region=REGION)
    assert result["recovered"] == 1
    assert row["phase"] == "done"
    assert row["next_attempt_at"] is None
    assert local is not None and local.state == SpendLeaseState.TOMBSTONED
    assert not local.open_allocations


def test_candidate_recovery_resumes_after_crash_immediately_after_ownership() -> None:
    store, database, ledger = _store()
    identity = _identity()
    _insert_candidate(store, identity)
    ledger.initialize(_local_candidate(identity), region=REGION)
    _allocate(ledger, identity)
    assert database.run_in_transaction(
        lambda transaction: take_recovery_ownership(
            transaction, store._param_types, identity.lease_id
        )
    ) == 1

    result = reconcile_spend_leases(store, now=NOW)

    assert result["recovered"] == 1
    assert database.spend_lease_open[identity.lease_id]["phase"] == "done"


def test_candidate_recovery_rereads_and_compensates_a_4b_cas_loser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, ledger = _store()
    identity = _identity()
    _insert_candidate(store, identity)
    calls = 0
    real_tombstone = BigtableSpendLeaseLedger.tombstone_unminted

    def lose_once(self: BigtableSpendLeaseLedger, lease_id: str, *, region: str, proof: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            _allocate(self, identity)
            raise SpendLeaseConflictError("late producer won the first CAS")
        return real_tombstone(self, lease_id, region=region, proof=proof)

    monkeypatch.setattr(BigtableSpendLeaseLedger, "tombstone_unminted", lose_once)

    result = reconcile_spend_leases(store, now=NOW)

    local = ledger.get(identity.lease_id, region=REGION)
    assert calls == 2
    assert result["recovered"] == 1
    assert local is not None and local.state == SpendLeaseState.TOMBSTONED
    assert not local.open_allocations
    assert database.spend_lease_open[identity.lease_id]["phase"] == "done"


def test_open_sweep_binds_as_last_resort_without_incrementing_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, database, ledger = _store()
    identity = _identity(expires_at=NOW + timedelta(minutes=5))
    _insert_candidate(store, identity)
    _open_candidate(store, identity)
    database.spend_lease_open[identity.lease_id]["next_attempt_at"] = NOW
    ledger.initialize(_local_candidate(identity), region=REGION)
    _allocate(ledger, identity)
    _seed_global(store, identity, state="ACTIVE")

    class Authorization:
        id = identity.creating_authorization_id
        spend_lease_id = identity.lease_id
        spend_lease_gen = identity.gen
        spend_lease_allocated_micro = 500
        finalization_outcome = None
        finalized_cost_microdollars = None
        settled = False

    monkeypatch.setattr(type(store), "get_gateway_authorization", lambda *_: Authorization())

    result = reconcile_spend_leases(store, now=NOW)

    local = ledger.get(identity.lease_id, region=REGION)
    row = database.spend_lease_open[identity.lease_id]
    assert local is not None
    assert local.allocations[0].binding_state == BindingState.COMMITTED
    assert result["deferred"] == 1
    assert row["attempts"] == 0


@pytest.mark.parametrize(
    ("slot", "global_state", "fence_count"),
    [
        (False, "ACTIVE", 1),
        (True, "DRAINING", 0),
        (True, "ACTIVE", 1),
    ],
    ids=["closing-guard", "fence-count-guard", "slot-owner-guard"],
)
def test_close_step_two_lost_guard_rolls_back_credit_release(
    slot: bool,
    global_state: str,
    fence_count: int,
) -> None:
    store, database, ledger = _store()
    identity = _identity()
    _insert_candidate(store, identity)
    _open_candidate(store, identity)
    ledger.initialize(
        _local_candidate(identity, state=SpendLeaseState.DRAINING), region=REGION
    )
    _seed_global(store, identity, state=global_state, slot=slot, count=fence_count)
    _seed_credit(database, identity)
    before = dict(database.typed["tr_credit_balance"][(identity.workspace_id, 0)])

    result = reconcile_spend_leases(store, now=NOW, max_attempts=99)

    assert result["errors"] == 1
    assert database.typed["tr_credit_balance"][(identity.workspace_id, 0)] == before
    assert database.spend_lease_open[identity.lease_id]["global_closed_at"] is None


@pytest.mark.parametrize("slot", [False, True], ids=["active-owner", "predecessor-owner"])
def test_close_step_two_and_three_close_global_then_local(slot: bool) -> None:
    store, database, ledger = _store()
    identity = _identity()
    _insert_candidate(store, identity)
    _open_candidate(store, identity)
    ledger.initialize(
        _local_candidate(identity, state=SpendLeaseState.DRAINING), region=REGION
    )
    _seed_global(store, identity, state="DRAINING", slot=slot, count=1)
    _seed_credit(database, identity)

    result = reconcile_spend_leases(store, now=NOW)

    credit = database.typed["tr_credit_balance"][(identity.workspace_id, 0)]
    global_body = store._read_entity(SPEND_LEASE_KIND, identity.lease_id, dict)
    local = ledger.get(identity.lease_id, region=REGION)
    open_row = database.spend_lease_open[identity.lease_id]
    assert result["closed"] == 1
    assert credit["reserved"] == 0
    assert global_body["state"] == "CLOSED"
    assert global_body["frozen_local_version"] == 0
    assert local is not None and local.state == SpendLeaseState.CLOSED
    assert open_row["global_closed_at"] == NOW
    assert open_row["local_closed_at"] == NOW
    if slot:
        fence = store._read_entity(
            FENCE_KIND,
            store._spend_lease_pair_id(identity.key_hash, identity.boot_kid),
            dict,
        )
        assert fence["open_predecessor_count"] == 0


def test_close_eligible_since_is_monotonic_and_contrary_state_is_dead(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, database, ledger = _store()
    identity = _identity()
    _insert_candidate(store, identity)
    _open_candidate(store, identity)
    ledger.initialize(
        _local_candidate(identity, state=SpendLeaseState.DRAINING), region=REGION
    )
    _seed_global(store, identity)
    _seed_credit(database, identity)
    database.spend_lease_open[identity.lease_id]["close_eligible_since"] = NOW - timedelta(hours=1)

    # Inject an impossible ACTIVE observation without changing the durable
    # timestamp; this is the contradiction decision 38 requires us to detect.
    impossible = _local_candidate(identity, state=SpendLeaseState.ACTIVE)
    monkeypatch.setattr(BigtableSpendLeaseLedger, "get", lambda *_args, **_kwargs: impossible)
    caplog.set_level(logging.ERROR)

    result = reconcile_spend_leases(store, now=NOW)

    row = database.spend_lease_open[identity.lease_id]
    assert result["dead"] == 1
    assert row["close_eligible_since"] == NOW - timedelta(hours=1)
    assert row["dead"] is True
    assert "spend_lease.reconcile_contradiction" in caplog.text


def test_lag_logs_eligibility_and_expired_open_age_including_dead(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store, database, _ledger = _store()
    identity = _identity()
    _insert_candidate(store, identity)
    _open_candidate(store, identity)
    row = database.spend_lease_open[identity.lease_id]
    row["next_attempt_at"] = None
    row["created_at"] = NOW - timedelta(hours=30)
    row["close_eligible_since"] = NOW - timedelta(hours=25)
    row["dead"] = True
    caplog.set_level(logging.INFO)

    result = reconcile_spend_leases(store, now=NOW)

    assert result["eligibility_lag_seconds"] == 25 * 3600
    assert result["open_age_lag_seconds"] == 30 * 3600
    assert "spend_lease.reconcile_lag_exceeded" in caplog.text
    assert "spend_lease.reconcile_dead_count" in caplog.text


def test_dead_row_requeue_resets_attempts_and_due_time() -> None:
    store, database, _ledger = _store()
    identity = _identity()
    _insert_candidate(store, identity)
    _open_candidate(store, identity)
    row = database.spend_lease_open[identity.lease_id]
    row.update({"dead": True, "attempts": 12, "last_error": "broken", "next_attempt_at": None})

    count = requeue_dead_spend_leases(store, now=NOW)

    assert count == 1
    assert row is not database.spend_lease_open[identity.lease_id]
    requeued = database.spend_lease_open[identity.lease_id]
    assert requeued["dead"] is False
    assert requeued["attempts"] == 0
    assert requeued["last_error"] is None
    assert requeued["next_attempt_at"] == NOW


def test_spend_lease_singleton_lock_fences_takeover_and_stale_release() -> None:
    store, _database, _ledger = _store()
    first = acquire_spend_lease_reconciler_lock(
        store, owner="slrec-first", ttl_seconds=30, now=NOW
    )
    busy = acquire_spend_lease_reconciler_lock(
        store, owner="slrec-second", ttl_seconds=30, now=NOW + timedelta(seconds=1)
    )
    takeover = acquire_spend_lease_reconciler_lock(
        store, owner="slrec-second", ttl_seconds=30, now=NOW + timedelta(seconds=31)
    )

    assert first is not None and first.fencing_token == 1
    assert busy is None
    assert takeover is not None and takeover.fencing_token == 2
    assert takeover.previous_owner == "slrec-first"
    assert not release_spend_lease_reconciler_lock(
        store,
        owner="slrec-first",
        fencing_token=first.fencing_token,
        now=NOW + timedelta(seconds=32),
    )
    assert release_spend_lease_reconciler_lock(
        store,
        owner="slrec-second",
        fencing_token=takeover.fencing_token,
        now=NOW + timedelta(seconds=32),
    )


def test_done_candidate_work_row_is_deleted_after_thirty_days_only() -> None:
    store, database, ledger = _store()
    identity = _identity()
    _insert_candidate(store, identity)
    ledger.initialize(_local_candidate(identity), region=REGION)
    reconcile_spend_leases(store, now=NOW)
    assert identity.lease_id in database.spend_lease_open

    result = reconcile_spend_leases(store, now=NOW + timedelta(days=31))

    assert result["deleted"] == 1
    assert identity.lease_id not in database.spend_lease_open
    # Decision 33 keeps never-committed candidate history in Bigtable.
    assert ledger.get(identity.lease_id, region=REGION) is not None


def test_closed_local_row_deletes_only_after_fence_no_longer_names_lease() -> None:
    store, database, ledger = _store()
    identity = _identity()
    _insert_candidate(store, identity)
    _open_candidate(store, identity)
    closed = _local_candidate(identity, state=SpendLeaseState.CLOSED)
    ledger.initialize(closed, region=REGION)
    _seed_global(store, identity, state="CLOSED")
    row = database.spend_lease_open[identity.lease_id]
    row.update(
        {
            "close_eligible_since": NOW - timedelta(days=31),
            "global_closed_at": NOW - timedelta(days=31),
            "local_closed_at": NOW - timedelta(days=31),
            "next_attempt_at": NOW,
        }
    )
    global_body = _global_body(identity, state="CLOSED")
    global_body["frozen_local_version"] = 0
    store._write_entity(SPEND_LEASE_KIND, identity.lease_id, global_body)

    kept = reconcile_spend_leases(store, now=NOW)

    assert kept["deferred"] == 1
    assert identity.lease_id in database.spend_lease_open
    store._write_entity(
        FENCE_KIND,
        store._spend_lease_pair_id(identity.key_hash, identity.boot_kid),
        {
            "lease_id": "successor",
            "gen": identity.gen + 1,
            "open_predecessor_count": 0,
            "lease_status": "open",
        },
    )
    database.spend_lease_open[identity.lease_id]["next_attempt_at"] = NOW

    deleted = reconcile_spend_leases(store, now=NOW)

    assert deleted["deleted"] == 1
    assert identity.lease_id not in database.spend_lease_open
    assert ledger.get(identity.lease_id, region=REGION) is None
