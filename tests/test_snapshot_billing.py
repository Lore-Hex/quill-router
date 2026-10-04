"""Authorization prices survive catalog refreshes on every money path."""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import BackgroundTasks
from starlette.requests import Request

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import _ParamTypes
from tests.test_home_settlement import _drain_with
from tests.test_settle_one_commit import (
    INTERNAL,
    MODEL,
    _money,
    _settle_body,
)
from tests.test_settle_one_commit import (
    fixed_catalog as fixed_catalog,
)
from tests.test_settle_one_commit import (
    prod_store as prod_store,
)
from trusted_router.catalog import MODEL_ENDPOINTS
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services.settle_outbox_apply import ApplyOutcome, apply_frozen_settle
from trusted_router.services.settle_outbox_drain import drain_settle_outbox
from trusted_router.stage_d import (
    billing_pricing_snapshot,
    canonical_pricing_snapshot,
    endpoint_pricing_document,
)
from trusted_router.storage import InMemoryStore, Workspace, configure_store
from trusted_router.storage_gcp_authorize import _finalize_reaped_reservation_atomic
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_models import GatewayAuthorization


def authorize(key: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kw: True)
    return gateway._authorize_gateway_sync(
        Request({"type": "http", "method": "POST", "path": "/", "headers": [
            (b"x-tr-boot-auth", b"kid=snapshot-test,sig=x"),
        ]}),
        GatewayAuthorizeRequest(
            api_key_hash=key.hash, model=MODEL, estimated_input_tokens=100,
            max_output_tokens=100, stream=True, route_type="chat.completions",
        ),
        INTERNAL.model_copy(update={"stage_d_eligibility_enabled": True, "stage_d_pilot_workspace_ids": ""}),
    )["data"]


def refresh(endpoint_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoint_id, replace(
        MODEL_ENDPOINTS[endpoint_id], prompt_price_microdollars_per_million_tokens=2_000_000,
    ))


@pytest.mark.parametrize("path", ["inline", "one_commit", "outbox_replay", "outbox_drain", "reaped"])
def test_snapshot_charge_survives_refresh_on_every_typed_path(
    prod_store: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch, path: str,
) -> None:
    store, db, key = prod_store
    authorized = authorize(key, monkeypatch)
    assert authorized["candidate_cost_reporting"] is True
    aid, rid = authorized["authorization_id"], authorized["credit_reservation_id"]
    refresh(authorized["endpoint_id"], monkeypatch)
    body = _settle_body(authorized).model_copy(update={"route_type": "chat.completions"})
    if path == "reaped":
        expired = datetime.now(UTC) - timedelta(seconds=1)
        db.reservations[rid]["expires_at"] = expired
        db.gateway_authorizations[aid].update(
            started_at=expired, selected_endpoint_id=authorized["endpoint_id"],
            delivered_usage=json.dumps({"input_tokens": 14, "output_tokens": 7}),
        )
        result = _finalize_reaped_reservation_atomic(
            db, _ParamTypes, reservation_id=rid, reap_now=datetime.now(UTC),
            guard_outbox=True, snapshot_booking_enabled=True,
            operational_analytics_outbox=None,
        )
        assert result.snapshot_booked is True
    else:
        if path != "one_commit":
            monkeypatch.setattr(type(store), "typed_settle_one_commit_result", lambda *a, **kw: None)
        original = type(store).typed_finalize_gateway_authorization_result
        if path.startswith("outbox"):
            def unavailable(*a: Any, **kw: Any) -> None:
                raise RuntimeError("transient finalize failure")
            monkeypatch.setattr(type(store), "typed_finalize_gateway_authorization_result", unavailable)
        data = gateway._settle_gateway_authorization(body, success=True, settings=INTERNAL)["data"]
        assert data["cost_microdollars"] == 49  # live catalog would charge 63
        if path.startswith("outbox"):
            assert data["disposition"] == "intent_durable"
            outbox = SpannerSettleOutbox(db, _ParamTypes)
            row = outbox.get(aid, "settle")
            assert row is not None and row.actual_cost_micro == 49
            monkeypatch.setattr(type(store), "typed_finalize_gateway_authorization_result", original)
            # A second refresh after enqueue cannot change replay/drain either.
            monkeypatch.setitem(MODEL_ENDPOINTS, authorized["endpoint_id"], replace(
                MODEL_ENDPOINTS[authorized["endpoint_id"]], request_price_microdollars=99,
            ))
            if path == "outbox_replay":
                assert apply_frozen_settle(row) == ApplyOutcome.SETTLED_NOW
                assert apply_frozen_settle(row) == ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
            else:
                db.settle_outbox[(aid, "settle")]["next_attempt_at"] = "2000-01-01T00:00:00Z"
                drained = drain_settle_outbox(1)
                assert drained["outcomes"] == {"settled_now": 1}
    assert db.reservations[rid]["actual_micro"] == 49
    assert _money(db, "ws-one-commit", key.hash) == (49, 0, 49, 0)
    stored = store.get_gateway_authorization(aid)
    assert stored is not None and stored.finalized_cost_microdollars == 49
    # Old gateways do not echo the new promise: the same settle request works.
    replay = gateway._settle_gateway_authorization(body, success=True, settings=INTERNAL)["data"]
    assert replay["cost_microdollars"] == 49
    assert replay["already_settled"] is True


def test_snapshot_charge_reaches_deferred_home_ledger(
    fixed_catalog: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    store._operational_analytics_outbox = None
    configure_store(store)
    snapshot = canonical_pricing_snapshot(endpoint_pricing_document((MODEL_ENDPOINTS[fixed_catalog],)))
    auth = store.create_gateway_authorization(
        workspace_id="home-workspace", key_hash="home-key", model_id=MODEL,
        provider="anthropic", usage_type="Credits", estimated_microdollars=600,
        endpoint_id=fixed_catalog, candidate_endpoint_ids=[fixed_catalog],
        credit_reservation_id=None, settlement="deferred_home",
        deferred_cap_microdollars=1000,
    )
    auth.pricing_snapshot = snapshot
    auth.stage_d_reason = "ok"
    store._run_transaction(lambda tx: store._write_entity_tx(tx, "gateway_authorization", auth.id, auth))
    refresh(fixed_catalog, monkeypatch)
    body = _settle_body({"authorization_id": auth.id, "endpoint_id": fixed_catalog})
    data = gateway._settle_gateway_authorization(
        body, success=True, settings=Settings(environment="test"), background_tasks=BackgroundTasks(),
    )["data"]
    assert data["cost_microdollars"] == 49
    assert conn.execute("SELECT cost_microdollars FROM tr_home_settlement_outbox").fetchall() == [(49,)]
    assert store.deferred_outstanding("home-workspace")["outstanding"] == 49

    home = InMemoryStore()
    home.workspaces["home-workspace"] = Workspace(id="home-workspace", name="Home", owner_user_id="owner")

    def apply_at_home(request: httpx.Request) -> httpx.Response:
        forwarded = json.loads(request.content)
        assert forwarded == {
            "authorization_id": auth.id, "workspace_id": "home-workspace", "cost_microdollars": 49,
        }
        outcome = home.apply_federated_usage(
            source_plane="peer", authorization_id=auth.id, workspace_id="home-workspace",
            cost_microdollars=forwarded["cost_microdollars"], daily_cap_microdollars=1000,
        )
        assert outcome == "applied"
        return httpx.Response(200, json={"data": {"outcome": outcome}})

    assert _drain_with(monkeypatch, store, apply_at_home)["forwarded"] == 1
    assert home.credit_money["home-workspace"].total_usage_microdollars == 49
    assert store.deferred_outstanding("home-workspace")["outstanding"] == 0
    assert _drain_with(monkeypatch, store, apply_at_home)["examined"] == 0
    assert home.credit_money["home-workspace"].total_usage_microdollars == 49


def eligible_auth(endpoint_id: str) -> GatewayAuthorization:
    return GatewayAuthorization(
        id="snapshot", workspace_id="workspace", key_hash="key", model_id=MODEL,
        provider="anthropic", usage_type="Credits", estimated_microdollars=600,
        endpoint_id=endpoint_id, candidate_endpoint_ids=[endpoint_id], stage_d_reason="ok",
        pricing_snapshot=canonical_pricing_snapshot(endpoint_pricing_document((MODEL_ENDPOINTS[endpoint_id],))),
    )


@pytest.mark.parametrize("change", [
    {"stage_d_reason": "pricing_kind"}, {"stage_d_reason": "route"},
    {"stage_d_reason": "service_tier"}, {"stage_d_reason": None},
    {"pricing_snapshot": None}, {"pricing_snapshot": "{}"}, {"pricing_snapshot": "null"},
    {"usage_type": "BYOK"}, {"custom_model_id": "wrapper"},
    {"user_provided_model_id": "owner"}, {"native_batch_eligible": True},
    {"video_pricing_snapshot": "video"}, {"additional_cost_reservation_microdollars": 100},
    {"receipt_fee_basis_points": 1200}, {"app_markup_basis_points": 100},
    {"custom_model_markup_basis_points": 100}, {"candidate_endpoint_ids": ["missing"]},
    {"endpoint_id": "missing"}, {"endpoint_id": None, "candidate_endpoint_ids": []},
])
def test_snapshot_eligibility_excludes_alternate_contracts(
    fixed_catalog: str, change: dict[str, Any],
) -> None:
    auth = eligible_auth(fixed_catalog)
    assert billing_pricing_snapshot(auth) is not None
    for field, value in change.items():
        setattr(auth, field, value)
    assert billing_pricing_snapshot(auth) is None


@pytest.mark.parametrize("field,value", [
    ("rounding", "ceil"), ("price_history_version", 2),
    ("request_fee_micro", -1), ("request_fee_micro", 2**63),
    ("request_fee_micro", True), ("rates", {}), ("tiers", [{}]),
    ("endpoint_id", "missing"),
])
def test_snapshot_requires_supported_complete_candidates(
    fixed_catalog: str, field: str, value: Any,
) -> None:
    auth = eligible_auth(fixed_catalog)
    document = json.loads(auth.pricing_snapshot or "")
    document["candidates"][0][field] = value
    auth.pricing_snapshot = canonical_pricing_snapshot(document)
    assert billing_pricing_snapshot(auth) is None


@pytest.mark.parametrize("reason", ["mixed_usage_type", "pricing_kind", "route", "service_tier", "not_streaming", None])
def test_ineligible_authorization_keeps_live_charge(
    prod_store: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch, reason: str | None,
) -> None:
    store, db, key = prod_store
    authorized = authorize(key, monkeypatch)
    aid = authorized["authorization_id"]
    payload = json.loads(db.gateway_authorizations[aid]["payload"])
    payload["stage_d_reason"] = reason
    db.gateway_authorizations[aid]["payload"] = json.dumps(payload)
    auth = store.get_gateway_authorization(aid)
    assert auth is not None and billing_pricing_snapshot(auth) is None
    refresh(authorized["endpoint_id"], monkeypatch)
    data = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=INTERNAL)["data"]
    assert data["cost_microdollars"] == 63
    assert _money(db, "ws-one-commit", key.hash) == (63, 0, 63, 0)


@pytest.mark.parametrize("route,additional", [
    ("fusion", 0), ("advisor", 0), ("combo", 0), ("responses.web_search.planner", 10),
])
def test_orchestration_and_search_keep_live_prices_and_no_promise(
    prod_store: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch,
    route: str, additional: int,
) -> None:
    _store, db, key = prod_store
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kw: True)
    body = GatewayAuthorizeRequest(
        api_key_hash=key.hash, model=MODEL, estimated_input_tokens=100, max_output_tokens=100,
        stream=True, route_type=route, additional_cost_reservation_microdollars=additional,
    )
    settings = INTERNAL.model_copy(update={
        "stage_d_eligibility_enabled": True, "stage_d_pilot_workspace_ids": "",
    })
    authorized = gateway._authorize_gateway_sync(
        Request({"type": "http", "method": "POST", "path": "/", "headers": [
            (b"x-tr-boot-auth", b"kid=snapshot-test,sig=x"),
        ]}), body, settings,
    )["data"]
    assert authorized["candidate_cost_reporting"] is False
    assert db.gateway_authorizations[authorized["authorization_id"]]["pricing_snapshot"] is None
    refresh(authorized["endpoint_id"], monkeypatch)
    settle = _settle_body(authorized).model_copy(update={
        "route_type": route, "additional_cost_microdollars": additional,
    })
    data = gateway._settle_gateway_authorization(settle, success=True, settings=INTERNAL)["data"]
    assert data["cost_microdollars"] == (73 if additional else 63)


@pytest.mark.parametrize("field,value,want", [
    ("receipt_fee_basis_points", 1200, 67),
    ("app_markup_basis_points", 1000, 69),
    ("custom_model_markup_basis_points", 1000, 69),
    ("pricing_snapshot", None, 63),
])
def test_surcharges_and_missing_snapshot_retain_live_billing(
    prod_store: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch,
    field: str, value: Any, want: int,
) -> None:
    store, db, key = prod_store
    authorized = authorize(key, monkeypatch)
    aid = authorized["authorization_id"]
    payload = json.loads(db.gateway_authorizations[aid]["payload"])
    payload[field] = value
    db.gateway_authorizations[aid]["payload"] = json.dumps(payload)
    if field == "pricing_snapshot":
        db.gateway_authorizations[aid][field] = value
    auth = store.get_gateway_authorization(aid)
    assert auth is not None and billing_pricing_snapshot(auth) is None
    refresh(authorized["endpoint_id"], monkeypatch)
    data = gateway._settle_gateway_authorization(_settle_body(authorized), success=True, settings=INTERNAL)["data"]
    assert data["cost_microdollars"] == want
    assert _money(db, "ws-one-commit", key.hash) == (want, 0, want, 0)


def test_duplicate_candidate_is_not_promised(fixed_catalog: str) -> None:
    auth = eligible_auth(fixed_catalog)
    document = json.loads(auth.pricing_snapshot or "")
    document["candidates"] *= 2
    auth.pricing_snapshot = canonical_pricing_snapshot(document)
    assert billing_pricing_snapshot(auth) is None


def test_served_fallback_uses_its_own_snapshot_and_cache_components(
    prod_store: tuple[Any, Any, Any], monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, db, key = prod_store
    authorized = authorize(key, monkeypatch)
    aid = authorized["authorization_id"]
    primary = MODEL_ENDPOINTS[authorized["endpoint_id"]]
    fallback = replace(primary, id=primary.id + "-fallback", request_price_microdollars=3,
                       prompt_price_microdollars_per_million_tokens=2_000_000,
                       completion_price_microdollars_per_million_tokens=7_000_000)
    monkeypatch.setitem(MODEL_ENDPOINTS, fallback.id, fallback)
    auth = store.get_gateway_authorization(aid)
    assert auth is not None
    auth.candidate_endpoint_ids.append(fallback.id)
    auth.pricing_snapshot = canonical_pricing_snapshot(endpoint_pricing_document((primary, fallback)))
    payload = json.loads(db.gateway_authorizations[aid]["payload"])
    payload["candidate_endpoint_ids"] = auth.candidate_endpoint_ids
    db.gateway_authorizations[aid].update(payload=json.dumps(payload), pricing_snapshot=auth.pricing_snapshot)
    assert billing_pricing_snapshot(auth) is not None
    refresh(fallback.id, monkeypatch)
    monkeypatch.setitem(MODEL_ENDPOINTS, fallback.id, replace(MODEL_ENDPOINTS[fallback.id], request_price_microdollars=99))
    body = _settle_body(authorized).model_copy(update={
        "selected_endpoint": fallback.id, "cache_read_input_tokens": 5,
        "cache_creation_input_tokens": 3,
    })
    data = gateway._settle_gateway_authorization(body, success=True, settings=INTERNAL)["data"]
    # Anthropic 14 uncached + 5 read + 3 creation: 28 + 1 + 8 + 49 + fee 3.
    assert data["cost_microdollars"] == 89
    assert _money(db, "ws-one-commit", key.hash) == (89, 0, 89, 0)


@pytest.mark.parametrize("provider,model,tier,want", [
    ("anthropic", MODEL, 14, 63),
    ("sakana", "sakana-ai/fugu-ultra-v1.1", 14, 35),
])
def test_reaped_pricing_uses_the_same_private_tier_contract(
    fixed_catalog: str, provider: str, model: str, tier: int, want: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router.storage_gcp_stage_d import delivered_usage_charge_microdollars

    endpoint = replace(MODEL_ENDPOINTS[fixed_catalog], model_id=model, provider=provider)
    monkeypatch.setitem(MODEL_ENDPOINTS, fixed_catalog, endpoint)
    auth = eligible_auth(fixed_catalog)
    auth.model_id = model
    auth.provider = provider
    document = json.loads(auth.pricing_snapshot or "")
    candidate = document["candidates"][0]
    low_rates = dict(candidate["rates"], input_micro_per_million=1_000_000, output_micro_per_million=1_000_000)
    high_rates = dict(low_rates, input_micro_per_million=2_000_000, output_micro_per_million=1_000_000)
    candidate["tiers"] = [{"max_prompt_tokens": 14, "rates": low_rates}, {"max_prompt_tokens": None, "rates": high_rates}]
    auth.pricing_snapshot = canonical_pricing_snapshot(document)
    # A private basis of 14 selects the low tier only on the pinned Fugu route.
    assert delivered_usage_charge_microdollars(auth, document, provider, fixed_catalog, {
        "input_tokens": 28, "output_tokens": 7, "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0, "price_tier_input_tokens": tier,
    }) == want
