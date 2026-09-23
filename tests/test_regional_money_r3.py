"""Global/regional money differential through real gateway and typed ledgers."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from tests.test_operational_analytics_direct import _load_drainer
from tests.test_regional_accounting_v2 import _totals
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.operational_analytics_direct import (
    OperationalOutboxRow,
    normalise_operational_event,
)
from trusted_router.regional_billing import GLOBAL_CHARGE_FIELD, LOCAL_CHARGE_FIELD
from trusted_router.regional_quota_ledger import InMemoryRegionalQuotaLedger
from trusted_router.routes.internal import gateway
from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle
from trusted_router.services.spend_lease_shadow_dispatch import SpendLeaseShadowDispatcher
from trusted_router.storage import configure_store
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox


@pytest.mark.parametrize("receipt", [False, True])
@pytest.mark.parametrize("case", ["one", "rounding_before", "rounding", "rounding_after", "cached", "priority", "priority_default", "fallback", "refund", "overrun", "drain"])
def test_gateway_regional_matches_global_exactly(
    monkeypatch: pytest.MonkeyPatch, receipt: bool, case: str,
) -> None:
    results = []
    # Include a no-receipt global oracle to independently assert the 11200/10550
    # ratio, so mutating the shared receipt helper cannot make both paths pass.
    for regional, wants_receipt in [(False, False), (False, receipt), (True, receipt)]:
        store, db, _ = make_fake_store(request_record_write_mode="typed", operational_analytics_outbox_enabled=True)
        store._regional_quota_ledger = InMemoryRegionalQuotaLedger()
        ws = store.create_workspace("owner", "R3", trial_credit_microdollars=100_000_000)
        _, key = store.create_api_key(workspace_id=ws.id, name="key", creator_user_id="owner")
        configure_store(store)
        dispatcher = SpendLeaseShadowDispatcher(lambda _id, _payload: None)
        monkeypatch.setattr(gateway, "_SPEND_LEASE_SHADOW_DISPATCHER", dispatcher)
        outbox = SpannerSettleOutbox(db, store._param_types)
        monkeypatch.setattr(gateway, "spanner_settle_outbox", lambda outbox=outbox: outbox)
        settings = Settings(environment="test", regional_quota_leases_enabled=regional,
                            regional_quota_lease_issuance_enabled=regional,
                            regional_quota_lease_pilot_workspace_ids=ws.id,
                            spend_lease_issuance_enabled=False, settle_outbox_enabled=True)
        client = TestClient(create_app(settings, configure_store_arg=False, init_observability=False))
        request = dict(api_key_hash=key.hash, model="anthropic/claude-opus-4.7",
                       estimated_input_tokens=1 if case in {"overrun", "drain"} else 1000,
                       max_output_tokens=1 if case in {"overrun", "drain"} else 100,
                       route_type="chat.completions", region="us-central1", inference_receipt=wants_receipt)
        if case == "fallback":
            request["models"] = ["anthropic/claude-haiku-4.5"]
        if case in {"priority", "priority_default"}:
            request["service_tier"] = "priority"
            request["model"] = "openai/gpt-5.6-sol"
        response = client.post("/v1/internal/gateway/authorize", json=request)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        auth = store.get_gateway_authorization(data["authorization_id"])
        assert auth.settlement == ("regional_lease" if regional else "local")
        assert auth.receipt_fee_basis_points == (1200 if wants_receipt else 0)
        if regional:
            local = store._regional_quota_ledger.get(auth.regional_lease_id, region=auth.region)
            assert local.holds[0].reserved_microdollars == auth.estimated_microdollars
        body: dict[str, Any] = dict(authorization_id=auth.id, actual_input_tokens=1,
                                    actual_output_tokens=0, request_id="r3", elapsed_seconds=1)
        if case.startswith("rounding"):
            body.update(actual_input_tokens={"rounding_before": 210, "rounding": 211, "rounding_after": 212}[case])
        elif case == "cached":
            body.update(actual_input_tokens=200, actual_output_tokens=7,
                        cache_read_input_tokens=100, cache_creation_input_tokens=50)
        elif case in {"overrun", "drain"}:
            body.update(actual_input_tokens=2000, actual_output_tokens=500)
        elif case in {"priority", "priority_default"}:
            body.update(service_tier="priority" if case == "priority" else "default", actual_input_tokens=100, actual_output_tokens=10)
        elif case == "fallback":
            fallback = next(c for c in data["route_candidates"] if c["model"] == "anthropic/claude-haiku-4.5")
            body.update(selected_endpoint=fallback["endpoint_id"], actual_input_tokens=211)
        elif case == "refund":
            body.update(error_status=503, error_type="provider_error", actual_input_tokens=500, actual_output_tokens=20)
        with monkeypatch.context() as fail_inline:
            # Fix the model charge at both sides of the exact 211 -> 224
            # receipt boundary. Other cases exercise real catalog/cache/tier prices.
            boundary = {"one": 1, "rounding_before": 210, "rounding": 211, "rounding_after": 212}.get(case)
            if boundary is not None:
                fail_inline.setattr(gateway, "_endpoint_cost_microdollars", lambda *a, amount=boundary, **kw: amount)
            if case == "drain":
                def fail(*args: Any, **kwargs: Any) -> Any:
                    raise RuntimeError("crash before finalize")
                fail_inline.setattr(type(store), "typed_finalize_gateway_authorization_result", fail)
            response = client.post("/v1/internal/gateway/refund" if case == "refund" else "/v1/internal/gateway/settle", json=body)
        assert response.status_code == 200, response.text
        kind = "refund" if case == "refund" else "settle"
        row = outbox.get(auth.id, kind)
        assert row is not None
        total = row.actual_cost_micro
        if case == "drain":
            assert response.json()["data"]["disposition"] == "intent_durable"
            assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
        if regional:
            frozen = json.loads(row.settle_body) if row.settle_body else None
            # Done intents may clear repair bodies: the drain case proves the
            # durable payload before it is applied; inline assertions use ledger.
            if frozen:
                assert frozen[LOCAL_CHARGE_FIELD] == min(total, auth.estimated_microdollars)
                assert frozen[GLOBAL_CHARGE_FIELD] == max(0, total - auth.estimated_microdollars)
            local = store._regional_quota_ledger.get(auth.regional_lease_id, region=auth.region)
            assert local.spent_microdollars == min(total, auth.estimated_microdollars)
            excess = max(0, total - auth.estimated_microdollars)
            assert _totals(db, ws.id, key.hash) == (excess,) * 5
            if case in {"overrun", "drain"}:
                assert excess > 0
            now = datetime.now(UTC) + timedelta(minutes=2)
            assert store.reconcile_regional_quota_leases(now=now)["errors"] == 0
            assert store.reconcile_regional_quota_leases(now=now)["errors"] == 0
            events = [r for r in db.operational_analytics_outbox if r["event_kind"] == "spend_lease_shadow"]
            assert len(events) == 1
            event = json.loads(events[0]["payload"])
            assert event["regional_overrun_microdollars"] == excess
            assert event["regional_actual_microdollars"] == total
            observation = OperationalOutboxRow(0, now, "spend_lease_shadow", event["event_id"], events[0]["payload"])
            canonical = normalise_operational_event(observation)[0].row
            assert canonical["regional_overrun_microdollars"] == excess
            assert _load_drainer().normalise_operational_event(observation)[0].row == canonical
        assert _totals(db, ws.id, key.hash) == (total,) * 5
        duplicate = client.post("/v1/internal/gateway/refund" if case == "refund" else "/v1/internal/gateway/settle", json=body)
        assert duplicate.status_code == 200, duplicate.text
        assert _totals(db, ws.id, key.hash) == (total,) * 5
        if case == "drain":
            assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
            assert _totals(db, ws.id, key.hash) == (total,) * 5
        results.append((auth.estimated_microdollars, total))
        dispatcher.close()
    base, global_, regional_ = results
    assert regional_ == global_
    if case in {"one", "rounding_before", "rounding", "rounding_after"}:
        assert base[1] == {"one": 1, "rounding_before": 210, "rounding": 211, "rounding_after": 212}[case]
    if receipt:
        assert global_ == tuple((v * 11200 + 10549) // 10550 for v in base)


@pytest.mark.parametrize("version", [1, 2])
@pytest.mark.parametrize("missing_hold", [False, True])
def test_overrun_differential_claims_excess_once_even_after_local_commit_failure(
    monkeypatch: pytest.MonkeyPatch, version: int, missing_hold: bool,
) -> None:
    from dataclasses import replace

    from tests.test_regional_accounting_v2 import _authorize, _setup
    from trusted_router import storage_gcp_authorize as finalize
    from trusted_router import storage_gcp_regional_quota as quota
    from trusted_router.storage_models import SettleOutboxRow

    total = 15_001
    oracle, oracle_db, oracle_key, args = _setup()
    outcome, oracle_auth = oracle.authorize_gateway_typed(
        **args, has_credit_candidate=True, reservation_usage_type="Credits", skip_key_limit=True,
    )
    assert outcome == "accepted"
    assert oracle.typed_finalize_gateway_authorization_result(
        oracle_auth.id, success=True, actual_microdollars=total, selected_usage_type="Credits",
    ).finalized
    expected = _totals(oracle_db, oracle_key.workspace_id, oracle_key.hash)
    assert expected == (total,) * 5
    store, db, key, args = _setup()
    if version == 1:
        original_grant = quota.grant_regional_quota_lease
        def grant(*a: Any, **kw: Any) -> Any:
            lease = replace(original_grant(*a, **kw), accounting_version=1)
            store._write_entity("regional_quota_lease", lease.entity_id, lease)
            return lease
        monkeypatch.setattr(quota, "grant_regional_quota_lease", grant)
    auth = _authorize(store, args)
    ledger = store._regional_quota_ledger
    if missing_hold:
        local = ledger.get(auth.regional_lease_id, region=auth.region)
        ledger._leases[(auth.region, auth.regional_lease_id)] = replace(local, holds=())
    configure_store(store)
    row = SettleOutboxRow(
        authorization_id=auth.id, intent_kind="settle", settle_origin="typed",
        actual_cost_micro=total, reservation_id=auth.credit_reservation_id,
        selected_endpoint_id="provider/model", model_id="model", selected_usage_type="Credits",
        settle_body=json.dumps(dict(authorization_id=auth.id, actual_input_tokens=1, actual_output_tokens=1,
                                    regional_local_microdollars=10_000, regional_global_microdollars=5_001)),
    )
    outbox = SpannerSettleOutbox(db, store._param_types)
    assert outbox.enqueue(row, preserve_existing=True) == "inserted"
    with monkeypatch.context() as crash:
        def fail(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("crash after regional CAS before global commit")
        crash.setattr(finalize, "mark_gateway_authorization_settled", fail)
        with pytest.raises(RuntimeError, match="crash after regional CAS"):
            store.typed_finalize_gateway_authorization_result(
                auth.id, success=True, actual_microdollars=total, selected_usage_type="Credits",
            )
    assert _totals(db, key.workspace_id, key.hash) == (0,) * 5
    # A retry with corrected actuals cannot change the excess after local CAS.
    changed = replace(row, actual_cost_micro=total + 1)
    assert outbox.enqueue(changed, preserve_existing=True) == "frozen"
    frozen = outbox.get(auth.id, "settle")
    assert frozen.actual_cost_micro == total
    assert frozen.settle_body == row.settle_body
    assert apply_frozen_settle(frozen) == ApplyOutcome.SETTLED_NOW
    assert apply_frozen_settle(frozen) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    local_total = 0 if missing_hold else 10_000
    inline_key = total if version == 1 or missing_hold else 5_001
    assert _totals(db, key.workspace_id, key.hash) == (total - local_total, *(inline_key,) * 4)
    now = datetime.now(UTC) + timedelta(minutes=2)
    assert store.reconcile_regional_quota_leases(now=now)["errors"] == 0
    assert _totals(db, key.workspace_id, key.hash) == expected
    assert apply_frozen_settle(frozen) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
    store.reconcile_regional_quota_leases(now=now)
    assert _totals(db, key.workspace_id, key.hash) == expected


@pytest.mark.parametrize("parts", [(9999, 5002), (10000, 5000), (15001, 0), (10000, None)])
def test_drain_rejects_corrupt_split_before_money_changes(parts: tuple[int, int | None]) -> None:
    from tests.test_regional_accounting_v2 import _authorize, _setup
    from trusted_router.storage_models import SettleOutboxRow

    store, db, key, args = _setup()
    auth = _authorize(store, args)
    configure_store(store)
    row = SettleOutboxRow(
        authorization_id=auth.id, intent_kind="settle", settle_origin="typed",
        actual_cost_micro=15001, reservation_id=auth.credit_reservation_id,
        selected_endpoint_id="provider/model", model_id="model", selected_usage_type="Credits",
        settle_body=json.dumps(dict(authorization_id=auth.id, actual_input_tokens=1, actual_output_tokens=1,
                                    regional_local_microdollars=parts[0], regional_global_microdollars=parts[1])),
    )
    assert apply_frozen_settle(row) == ApplyOutcome.INVALID_ROW
    assert _totals(db, key.workspace_id, key.hash) == (0,) * 5
    assert not store.read_typed_reservation(auth.credit_reservation_id)["settled"]


def test_terminal_recovery_restores_only_lease_backed_charge() -> None:
    from dataclasses import replace

    from tests.test_regional_accounting_v2 import _authorize, _setup
    from trusted_router.storage_gcp_regional_quota import (
        get_global_regional_quota_lease,
        terminal_regional_hold_amount,
    )

    store, db, key, args = _setup()
    auth = _authorize(store, args)
    ledger = store._regional_quota_ledger
    reserved = ledger.get(auth.regional_lease_id, region=auth.region)
    assert store.typed_finalize_gateway_authorization_result(
        auth.id, success=True, actual_microdollars=15001, selected_usage_type="Credits",
    ).finalized
    lease = get_global_regional_quota_lease(
        store, workspace_id=key.workspace_id, region=auth.region, lease_id=auth.regional_lease_id,
    )
    assert terminal_regional_hold_amount(store, lease, auth.id) == 10000
    # Simulate loss of a local terminal transition after the typed outcome was
    # committed. Recovery must not put the already-booked excess in the lease.
    ledger._leases[(auth.region, auth.regional_lease_id)] = replace(reserved)
    now = datetime.now(UTC) + timedelta(hours=3)
    assert store.reconcile_regional_quota_leases(now=now)["errors"] == 0
    assert _totals(db, key.workspace_id, key.hash) == (15001,) * 5
    store.reconcile_regional_quota_leases(now=now)
    assert _totals(db, key.workspace_id, key.hash) == (15001,) * 5


def test_reaper_winning_overrun_matches_global_through_replay_and_drain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.test_regional_accounting_v2 import _authorize, _setup
    from trusted_router.services import settle_outbox_drain as drain
    from trusted_router.storage_gcp_authorize import (
        SettleOutcome,
        _finalize_reaped_reservation_atomic,
    )
    from trusted_router.storage_models import SettleOutboxRow

    results = []
    for regional in (False, True):
        store, db, key, args = _setup()
        if regional:
            auth = _authorize(store, args)
        else:
            outcome, auth = store.authorize_gateway_typed(
                **args, has_credit_candidate=True, reservation_usage_type="Credits", skip_key_limit=True,
            )
            assert outcome == "accepted"
        assert auth.estimated_microdollars == 10_000
        configure_store(store)
        reap_at = args["expires_at"] + timedelta(seconds=1)
        reaped = _finalize_reaped_reservation_atomic(
            db, store._param_types, reservation_id=auth.credit_reservation_id,
            reap_now=reap_at, guard_outbox=True, snapshot_booking_enabled=False,
            operational_analytics_outbox=None,
        )
        assert reaped.outcome == SettleOutcome.SETTLED and not reaped.snapshot_booked
        outbox = SpannerSettleOutbox(db, store._param_types)
        row = SettleOutboxRow(
            authorization_id=auth.id, intent_kind="settle", settle_origin="typed",
            actual_cost_micro=15_001, reservation_id=auth.credit_reservation_id,
            selected_endpoint_id="provider/model", model_id="model", selected_usage_type="Credits",
            settle_body=json.dumps(dict(authorization_id=auth.id, actual_input_tokens=1, actual_output_tokens=1)),
        )
        assert outbox.enqueue(row, preserve_existing=True) == "inserted"
        for _ in range(2):
            assert not store.typed_finalize_gateway_authorization_result(
                auth.id, success=True, actual_microdollars=15_001, selected_usage_type="Credits",
            ).finalized
            assert store.read_typed_reservation(auth.credit_reservation_id)["actual_micro"] == 0
            if regional:
                local = store._regional_quota_ledger.get(auth.regional_lease_id, region=auth.region)
                assert local.spent_microdollars == 0
                assert store.reconcile_regional_quota_leases(now=reap_at)["errors"] == 0
            assert _totals(db, key.workspace_id, key.hash) == (0,) * 5
            assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_RELEASED_FREE
        monkeypatch.setattr(drain, "spanner_settle_outbox", lambda outbox=outbox: outbox)
        monkeypatch.setattr(drain, "ops_alert", lambda *a, **kw: None)
        drain.drain_settle_outbox(limit=10)
        assert outbox.get(auth.id, "settle").status == "dead"
        drain.drain_settle_outbox(limit=10)
        assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_RELEASED_FREE
        if regional:
            assert store.reconcile_regional_quota_leases(now=reap_at)["errors"] == 0
        results.append(_totals(db, key.workspace_id, key.hash))
        assert sum(r["reserved"] for r in db.typed["tr_credit_balance"].values()) == 0
    assert results == [(0,) * 5, (0,) * 5]


@pytest.mark.parametrize("failed_compensation", [False, True], ids=["drain-only", "failed-inline-compensation"])
def test_reaper_zero_recovers_escrow_despite_dead_late_intent(
    monkeypatch: pytest.MonkeyPatch, failed_compensation: bool,
) -> None:
    from tests.test_regional_accounting_v2 import _authorize, _setup
    from trusted_router.services import settle_outbox_drain as drain
    from trusted_router.services.regional_quota_leases import HoldState, LeaseState
    from trusted_router.storage_gcp_authorize import (
        SettleOutcome,
        _finalize_reaped_reservation_atomic,
    )
    from trusted_router.storage_gcp_regional_quota import get_global_regional_quota_lease
    from trusted_router.storage_models import SettleOutboxRow

    store, db, key, args = _setup()
    auth = _authorize(store, args)
    configure_store(store)
    ledger = store._regional_quota_ledger
    reap_at = args["expires_at"] + timedelta(seconds=1)
    reaped = _finalize_reaped_reservation_atomic(
        db, store._param_types, reservation_id=auth.credit_reservation_id,
        reap_now=reap_at, guard_outbox=True, snapshot_booking_enabled=False,
        operational_analytics_outbox=None,
    )
    assert reaped.outcome == SettleOutcome.SETTLED and not reaped.snapshot_booked
    outbox = SpannerSettleOutbox(db, store._param_types)
    row = SettleOutboxRow(
        authorization_id=auth.id, intent_kind="settle", settle_origin="typed",
        actual_cost_micro=15_001, reservation_id=auth.credit_reservation_id,
        selected_endpoint_id="provider/model", model_id="model", selected_usage_type="Credits",
        settle_body=json.dumps(dict(authorization_id=auth.id, actual_input_tokens=1, actual_output_tokens=1,
                                    regional_local_microdollars=10_000, regional_global_microdollars=5_001)),
    )
    assert outbox.enqueue(row, preserve_existing=True) == "inserted"
    if failed_compensation:
        calls = []
        with monkeypatch.context() as unavailable:
            def fail(*a: Any, **kw: Any) -> Any:
                calls.append(kw)
                raise RuntimeError("local ledger unavailable during inline compensation")
            unavailable.setattr(type(store), "_finalize_regional_quota_hold", fail)
            assert not store.typed_finalize_gateway_authorization_result(
                auth.id, success=True, actual_microdollars=15_001, selected_usage_type="Credits",
            ).finalized
        assert calls == [dict(success=False, actual_microdollars=0)]
    # Otherwise the process crashed after enqueue, BEFORE any inline finalize.
    local = ledger.get(auth.regional_lease_id, region=auth.region)
    assert local.holds[0].state == HoldState.RESERVED
    grant = get_global_regional_quota_lease(
        store, workspace_id=key.workspace_id, region=auth.region, lease_id=auth.regional_lease_id,
    )
    assert sum(r["reserved"] for r in db.typed["tr_credit_balance"].values()) == grant.granted_microdollars > 0
    monkeypatch.setattr(drain, "spanner_settle_outbox", lambda: outbox)
    monkeypatch.setattr(drain, "ops_alert", lambda *a, **kw: None)
    result = drain.drain_settle_outbox(limit=10)
    assert result["outcomes"] == {ApplyOutcome.ALREADY_RELEASED_FREE: 1}
    assert outbox.get(auth.id, "settle").status == "dead"
    assert ledger.get(auth.regional_lease_id, region=auth.region).holds[0].state == HoldState.RESERVED
    for _ in range(2):
        assert store.reconcile_regional_quota_leases(now=reap_at)["errors"] == 0
        local = ledger.get(auth.regional_lease_id, region=auth.region)
        assert local.state == LeaseState.CLOSED
        assert local.holds[0].state == HoldState.REFUNDED
        assert local.spent_microdollars == local.reserved_microdollars == 0
        assert get_global_regional_quota_lease(
            store, workspace_id=key.workspace_id, region=auth.region, lease_id=auth.regional_lease_id,
        ).state == "closed"
        assert sum(r["reserved"] for r in db.typed["tr_credit_balance"].values()) == 0
        assert _totals(db, key.workspace_id, key.hash) == (0,) * 5
        assert store.read_typed_reservation(auth.credit_reservation_id)["actual_micro"] == 0
        assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_RELEASED_FREE
        drain.drain_settle_outbox(limit=10)
        assert outbox.get(auth.id, "settle").status == "dead"


@pytest.mark.parametrize(
    ("settled_time", "replay_time", "windows"),
    [
        (datetime(2027, 1, 10, 23, 59, tzinfo=UTC), datetime(2027, 1, 11, 0, 1, tzinfo=UTC), (0, 0, 15_001)),
        (datetime(2027, 3, 31, 23, 59, tzinfo=UTC), datetime(2027, 4, 1, 0, 1, tzinfo=UTC), (0, 15_001, 0)),
    ],
    ids=["sunday-monday", "month-boundary"],
)
@pytest.mark.parametrize("resharded", [False, True])
@pytest.mark.parametrize("writer_advanced", [False, True], ids=["fresh-clock", "cross-writer-advance"])
def test_overrun_cross_boundary_crash_replay_uses_one_settlement_time(
    monkeypatch: pytest.MonkeyPatch, settled_time: datetime, replay_time: datetime,
    windows: tuple[int, ...], resharded: bool, writer_advanced: bool,
) -> None:
    from tests.test_regional_accounting_v2 import _authorize, _current_totals, _setup
    from trusted_router import storage_gcp_authorize as finalize
    from trusted_router import storage_gcp_regional_quota as quota
    from trusted_router.spend_windows import window_floors
    from trusted_router.storage_gcp_counter_dml import release_key
    from trusted_router.storage_models import SettleOutboxRow

    # Compare the same settlement event at its authoritative booking time.
    # The global retry is a no-op; the regional retry finishes its split booking.
    monkeypatch.setattr(finalize, "utcnow", lambda: settled_time)
    oracle, oracle_db, oracle_key, args = _setup()
    outcome, oracle_auth = oracle.authorize_gateway_typed(
        **args, has_credit_candidate=True, reservation_usage_type="Credits", skip_key_limit=True,
    )
    assert outcome == "accepted"
    assert oracle.typed_finalize_gateway_authorization_result(
        oracle_auth.id, success=True, actual_microdollars=15_001, selected_usage_type="Credits",
    ).finalized

    store, db, key, args = _setup()
    monkeypatch.setattr("trusted_router.storage_gcp.randomized_credit_shards", lambda _n: [7])
    auth = _authorize(store, args)
    monkeypatch.setattr("trusted_router.services.regional_quota_leases._utc_now", lambda: settled_time)
    configure_store(store)
    row = SettleOutboxRow(
        authorization_id=auth.id, intent_kind="settle", settle_origin="typed",
        actual_cost_micro=15_001, reservation_id=auth.credit_reservation_id,
        selected_endpoint_id="provider/model", model_id="model", selected_usage_type="Credits",
        settle_body=json.dumps(dict(authorization_id=auth.id, actual_input_tokens=1, actual_output_tokens=1,
                                    regional_local_microdollars=10_000, regional_global_microdollars=5_001)),
    )
    outbox = SpannerSettleOutbox(db, store._param_types)
    assert outbox.enqueue(row, preserve_existing=True) == "inserted"
    with monkeypatch.context() as crash:
        def fail(*a: Any, **kw: Any) -> Any:
            raise RuntimeError("crash after CAS before Spanner commit")
        crash.setattr(finalize, "mark_gateway_authorization_settled", fail)
        with pytest.raises(RuntimeError, match="crash after CAS"):
            store.typed_finalize_gateway_authorization_result(
                auth.id, success=True, actual_microdollars=15_001, selected_usage_type="Credits",
            )
    local = store._regional_quota_ledger.get(auth.regional_lease_id, region=auth.region)
    assert local.spent_microdollars == 10_000
    assert local.holds[0].settled_at == settled_time
    assert not store.read_typed_reservation(auth.credit_reservation_id)["settled"]
    assert _totals(db, key.workspace_id, key.hash) == (0,) * 5
    # Even after expiry, the durable intent prevents a reaper from claiming
    # the reservation left open by the aborted Spanner transaction.
    reaped = finalize._finalize_reaped_reservation_atomic(
        db, store._param_types, reservation_id=auth.credit_reservation_id,
        reap_now=replay_time, guard_outbox=True, snapshot_booking_enabled=False,
        operational_analytics_outbox=None,
    )
    assert reaped.outcome != finalize.SettleOutcome.SETTLED
    if resharded:
        for identity in list(db.typed["tr_key_limit"]):
            if identity[0] == key.hash and identity[1] != 0:
                del db.typed["tr_key_limit"][identity]
    monkeypatch.setattr(finalize, "utcnow", lambda: replay_time)
    monkeypatch.setattr(quota, "utcnow", lambda: replay_time)
    monkeypatch.setattr("trusted_router.services.regional_quota_leases._utc_now", lambda: replay_time)
    if writer_advanced:
        # Another request commits the new window before this replay samples
        # stale floors. Use real counter DML for both sides of the differential.
        for target, target_auth, target_key in [(oracle, oracle_auth, oracle_key), (store, auth, key)]:
            shard = (0 if target is store and resharded else
                     target.read_typed_reservation(target_auth.credit_reservation_id)["key_shard"])
            assert target._run_in_transaction(lambda tx, target=target, target_key=target_key, shard=shard: release_key(
                tx, target._param_types, target_key.hash, 0, 100, book_to_byok=False,
                window_floors=window_floors(replay_time), shard=shard,
            )) == 1
    expected = tuple(v + (100 if writer_advanced else 0) for v in (15_001, *windows))
    assert _current_totals(oracle_db, oracle_key, replay_time) == expected
    assert not oracle.typed_finalize_gateway_authorization_result(
        oracle_auth.id, success=True, actual_microdollars=15_001, selected_usage_type="Credits",
    ).finalized
    attempts = []
    if writer_advanced:
        def clock() -> datetime:
            at = settled_time if not attempts else replay_time
            attempts.append(at)
            return at
        monkeypatch.setattr(finalize, "utcnow", clock)
    assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
    assert store.reconcile_regional_quota_leases(now=replay_time)["errors"] == 0
    for _ in range(2):
        assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
        assert store.reconcile_regional_quota_leases(now=replay_time)["errors"] == 0
        assert _current_totals(db, key, replay_time) == expected
        assert _totals(db, key.workspace_id, key.hash)[0] == 15_001
    if writer_advanced:
        assert attempts == [settled_time, replay_time]
