"""The S1 snapshot has the same money/evidence contract as the S9 reread."""
from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any

import pytest

from tests.test_regional_accounting_v2 import _authorize, _setup, _totals
from trusted_router import storage_gcp_authorize as finalize
from trusted_router.services.regional_quota_leases import (
    LeaseFenceMismatchError,
    LeaseSettlementError,
    LeaseState,
)
from trusted_router.storage_models import Generation
from trusted_router.types import UsageType


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("case", [
    "active", "expired", "draining", "quarantined", "missing_hold", "refund_race",
    "reaper", "replay", "concurrent", "refunded_hold", "changed_fence",
    "local_commit_retry",
])
def test_regional_snapshot_matches_reread(
    monkeypatch: pytest.MonkeyPatch, version: int, case: str,
) -> None:
    observations = []
    for use_snapshot in (False, True):
        with monkeypatch.context() as patch:
            store, db, key, args = _setup()
            if version == 1:
                from trusted_router import storage_gcp_regional_quota as quota

                grant = quota.grant_regional_quota_lease

                def v1(*a: Any, grant: Any = grant, store: Any = store, **kw: Any) -> Any:
                    lease = replace(grant(*a, **kw), accounting_version=1)
                    store._write_entity("regional_quota_lease", lease.entity_id, lease)
                    return lease

                patch.setattr(quota, "grant_regional_quota_lease", v1)
            auth = _authorize(store, args)
            snapshot = copy.deepcopy(store.get_gateway_authorization(auth.id))
            ledger = store._regional_quota_ledger
            local = ledger.get(auth.regional_lease_id, region=auth.region)
            now = datetime.now(UTC)
            if case == "expired":
                # Expiry stops admission, not settlement of an existing hold.
                patch.setattr("trusted_router.services.regional_quota_leases._utc_now",
                              lambda local=local: local.expires_at + timedelta(seconds=1))
            elif case == "draining":
                ledger.begin_drain(local.lease_id, region=local.region,
                                   fencing_token=local.fencing_token)
            elif case in {"quarantined", "missing_hold", "changed_fence"}:
                updates: dict[str, Any] = (
                    {"state": LeaseState.QUARANTINED} if case == "quarantined" else
                    {"holds": ()} if case == "missing_hold" else
                    {"fencing_token": local.fencing_token + 1}
                )
                ledger._leases[(local.region, local.lease_id)] = replace(local, **updates)
            elif case == "refunded_hold":
                ledger.refund(local.lease_id, region=local.region,
                              hold_id=auth.regional_hold_id, fencing_token=local.fencing_token)
            elif case == "refund_race":
                assert store.typed_finalize_gateway_authorization_result(
                    auth.id, success=False, actual_microdollars=0,
                    selected_usage_type="Credits",
                ).finalized
            elif case == "reaper":
                result = finalize._finalize_reaped_reservation_atomic(
                    db, store._param_types, reservation_id=auth.credit_reservation_id,
                    reap_now=args["expires_at"] + timedelta(seconds=1),
                    guard_outbox=True, snapshot_booking_enabled=False,
                    operational_analytics_outbox=None,
                )
                assert result.outcome == finalize.SettleOutcome.SETTLED
            generation = Generation.from_settle_body(
                authorization=auth, provider_name="provider", model_id="model",
                usage_type=UsageType.CREDITS, provider="provider", body={},
                input_tokens=5, output_tokens=7, actual_cost_microdollars=15_001,
            )

            settle = partial(
                store.typed_finalize_gateway_authorization_result,
                auth.id, success=True, actual_microdollars=15_001,
                selected_usage_type="Credits", generation=generation,
                authorization_snapshot=snapshot if use_snapshot else None,
            )

            if case in {"changed_fence", "refunded_hold"}:
                with pytest.raises(LeaseFenceMismatchError if case == "changed_fence" else LeaseSettlementError):
                    settle()
                assert not store.read_typed_reservation(auth.credit_reservation_id)["settled"]
                assert _totals(db, key.workspace_id, key.hash) == (0,) * 5
                outcomes = [False]
            else:
                if case == "local_commit_retry":
                    with patch.context() as crash:
                        def fail(*a: Any, **kw: Any) -> Any:
                            raise RuntimeError("after local CAS")
                        crash.setattr(finalize, "mark_gateway_authorization_settled", fail)
                        with pytest.raises(RuntimeError, match="after local CAS"):
                            settle()
                    assert not store.read_typed_reservation(auth.credit_reservation_id)["settled"]
                if case == "concurrent":
                    db._ready_barrier = threading.Barrier(2)
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        outcomes = sorted(pool.map(lambda _, settle=settle: settle().finalized, range(2)))
                    db._ready_barrier = None
                    assert outcomes == [False, True]
                    assert db.aborts > 0
                else:
                    outcomes = [settle().finalized]
                    assert outcomes == [case not in {"refund_race", "reaper"}]
                if case == "replay":
                    assert not settle().finalized
                # Reconciliation changes lease/escrow state, never the binding.
                assert store.reconcile_regional_quota_leases(now=now + timedelta(hours=3))["errors"] == 0
                expected = 0 if case in {"refund_race", "reaper"} else 15_001
                assert _totals(db, key.workspace_id, key.hash) == (expected,) * 5
            committed = store.get_gateway_authorization(auth.id)
            assert asdict(snapshot) == asdict(auth), "finalize mutated the request snapshot"
            expected_auth = copy.deepcopy(auth)
            if committed.settled:
                expected_auth.record_finalization(
                    success=case not in {"refund_race", "reaper"},
                    actual_microdollars=15_001,
                    selected_usage_type="Credits",
                    generation=None if case in {"refund_race", "reaper"} else generation,
                )
            assert asdict(committed) == asdict(expected_auth)
            local = ledger.get(auth.regional_lease_id, region=auth.region)
            observations.append((
                outcomes, _totals(db, key.workspace_id, key.hash),
                committed.finalization_outcome, committed.finalized_cost_microdollars,
                committed.finalized_generation_id, local.state,
                [(h.state, h.actual_microdollars) for h in local.holds],
            ))
    assert observations[0] == observations[1]


@pytest.mark.parametrize("field,value", [
    ("heartbeat_seq", 9), ("heartbeat_at", "2026-09-26T01:00:00Z"),
    ("heartbeat_hash", "f" * 64), ("started_at", "2026-09-26T00:59:00Z"),
    ("selected_endpoint_id", "provider/selected"),
    ("delivered_usage", '{"input_tokens":5,"output_tokens":7}'),
])
@pytest.mark.parametrize("storage", ["typed", "payload"])
def test_regional_snapshot_preserves_each_live_heartbeat_field(
    field: str, value: Any, storage: str,
) -> None:
    store, db, _key, args = _setup()
    auth = _authorize(store, args)
    snapshot = copy.deepcopy(auth)
    # Isolate each mutable column, including rolling payload-only writers.
    row = db.gateway_authorizations[auth.id]
    if storage == "typed":
        row[field] = datetime.fromisoformat(value.replace("Z", "+00:00")) if field.endswith("_at") else value
    else:
        payload = json.loads(row["payload"])
        payload[field] = value
        row["payload"] = json.dumps(payload)
    assert store.typed_finalize_gateway_authorization_result(
        auth.id, success=True, actual_microdollars=7_500,
        selected_usage_type="Credits", authorization_snapshot=snapshot,
    ).finalized
    assert getattr(store.get_gateway_authorization(auth.id), field) == value
    assert json.loads(db.gateway_authorizations[auth.id]["payload"])[field] == value
    assert getattr(snapshot, field) is None


def test_regional_authorization_terminal_update_still_guards_live_row() -> None:
    store, db, key, args = _setup()
    auth = _authorize(store, args)
    # A corrupt/legacy terminal row with an unclaimed reservation must abort
    # global billing, even though the request-local object says active.
    db.gateway_authorizations[auth.id]["settled"] = True
    with pytest.raises(RuntimeError, match="typed finalize failed"):
        store.typed_finalize_gateway_authorization_result(
            auth.id, success=True, actual_microdollars=7_500,
            selected_usage_type="Credits", authorization_snapshot=auth,
        )
    assert not store.read_typed_reservation(auth.credit_reservation_id)["settled"]
    assert _totals(db, key.workspace_id, key.hash) == (0,) * 5


@pytest.mark.parametrize("case", ["active", "expired", "refund_race", "reaper", "replay", "concurrent"])
def test_spend_snapshot_matches_reread(case: str) -> None:
    from tests.test_spend_lease_authorize import _authorize_store, _store_binding_harness
    from trusted_router.services.spend_lease_settlement import clamp_spend_lease_charge

    observations = []
    for use_snapshot in (False, True):
        store, db, key, plan, ledger = _store_binding_harness()
        outcome, auth = _authorize_store(store, key.hash, plan)
        assert outcome == "accepted" and auth is not None
        assert auth.settlement == "spend_lease"
        snapshot = copy.deepcopy(store.get_gateway_authorization(auth.id))
        if case == "expired":
            # The external lease can expire independently of its immutable
            # admission binding. The already-reserved request can still settle.
            lease = ledger.leases[auth.spend_lease_id]
            ledger.leases[auth.spend_lease_id] = replace(
                lease, expires_at=datetime.now(UTC) - timedelta(seconds=1),
            )
        elif case == "refund_race":
            assert store.typed_finalize_gateway_authorization_result(
                auth.id, success=False, actual_microdollars=0, selected_usage_type="Credits",
            ).finalized
        elif case == "reaper":
            result = finalize._finalize_reaped_reservation_atomic(
                db, store._param_types, reservation_id=auth.credit_reservation_id,
                reap_now=datetime.now(UTC) + timedelta(hours=3), guard_outbox=True,
                snapshot_booking_enabled=False, operational_analytics_outbox=None,
            )
            assert result.outcome == finalize.SettleOutcome.SETTLED
        cost = clamp_spend_lease_charge(snapshot, 15_001)
        assert cost == 500

        settle = partial(
            store.typed_finalize_gateway_authorization_result,
            auth.id, success=True, actual_microdollars=cost, selected_usage_type="Credits",
            authorization_snapshot=snapshot if use_snapshot else None,
        )

        if case == "concurrent":
            db._ready_barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = sorted(pool.map(lambda _, settle=settle: settle().finalized, range(2)))
            db._ready_barrier = None
            assert outcomes == [False, True]
            assert db.aborts > 0
        else:
            outcomes = [settle().finalized]
            assert outcomes == [case not in {"refund_race", "reaper"}]
        if case == "replay":
            assert not settle().finalized
        expected = 0 if case in {"refund_race", "reaper"} else cost
        assert _totals(db, auth.workspace_id, key.hash) == (expected,) * 5
        committed = store.get_gateway_authorization(auth.id)
        expected_auth = copy.deepcopy(auth)
        expected_auth.record_finalization(
            success=expected > 0, actual_microdollars=cost,
            selected_usage_type="Credits", generation=None,
        )
        assert asdict(committed) == asdict(expected_auth)
        assert asdict(snapshot) == asdict(auth)
        observations.append((outcomes, committed.finalization_outcome,
                             committed.finalized_cost_microdollars,
                             _totals(db, auth.workspace_id, key.hash)))
    assert observations[0] == observations[1]
