from __future__ import annotations

import json
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from trusted_router import billing_snapshot, catalog_ingest, gateway_timing, routing
from trusted_router.async_settle_ticket import TicketClaims
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
from trusted_router.catalog_data import PROVIDERS, Model
from trusted_router.config import Settings
from trusted_router.pricing import _PRICE_FLOOR_MICRODOLLARS_PER_M, _flat_tier
from trusted_router.routes.internal import gateway
from trusted_router.schemas import GatewayAuthorizeRequest, GatewaySettleRequest
from trusted_router.stage_d import canonical_pricing_snapshot, endpoint_pricing_document
from trusted_router.storage import InMemoryStore, configure_store

RERANK = "voyage/fixture-rerank"
EMBED = "voyage/fixture-embedding"
CHAT = "voyage/fixture-chat"


@pytest.fixture(autouse=True)
def fixed_timings(monkeypatch):
    monkeypatch.setattr(gateway_timing, "perf_counter", lambda: 0.0)


def request() -> Request:
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def models(monkeypatch, price=52_750):
    monkeypatch.setitem(PROVIDERS, "voyage", replace(PROVIDERS["voyage"], supports_rerank=True))
    result = {}
    for model_id in (RERANK, EMBED, CHAT):
        model = Model(
            id=model_id, name=model_id, provider="voyage", context_length=2_000_000,
            upstream_id=model_id.split("/")[1], supports_chat=model_id == CHAT,
            supports_embeddings=model_id == EMBED, supports_rerank=model_id == RERANK,
            prepaid_available=True, byok_available=False,
            prompt_price_microdollars_per_million_tokens=price,
            completion_price_microdollars_per_million_tokens=0,
            published_prompt_price_microdollars_per_million_tokens=price,
            published_completion_price_microdollars_per_million_tokens=0,
            price_tiers=_flat_tier(price, 0), published_price_tiers=_flat_tier(price, 0),
        )
        monkeypatch.setitem(MODELS, model_id, model)
        endpoint = next(iter(catalog_ingest._build_endpoints({model_id: model}).values()))
        monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)
        result[model_id] = endpoint
    return result


def seed(state="open"):
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user("rerank@example.com", trial_credit_microdollars=0)
    workspace = store.list_workspaces_for_user(user.id)[0]
    store.credit_workspace_once(workspace.id, 10_000_000 if state != "empty" else 0, "seed")
    _, key = store.create_api_key(
        workspace_id=workspace.id, name="fixture", creator_user_id=user.id,
        limit_microdollars=1 if state == "key-limit" else None,
        limit_daily_microdollars=1 if state == "daily-limit" else None,
    )
    if state == "spent":
        key.usage_microdollars = 10_000_000
        key.limit_microdollars = 10_000_000
    return store, workspace, key


def authorize(key, model, route, tokens=100, **changes):
    return gateway._authorize_gateway_sync(request(), GatewayAuthorizeRequest(
        api_key_hash=key.hash, model=model, route_type=route,
        estimated_input_tokens=tokens, max_output_tokens=1, idempotency_key="fixture",
        **changes,
    ), Settings(environment="test"))["data"]


def error(message):
    return {
        "error": {"code": 400, "message": message, "type": "model_not_supported", "source": "router"},
        "data": {"timing": {name: 0 for name in (
            "total_ms", "spanner_rpcs", "key_lookup_ms", "routing_ms", "store_ms", "post_commit_ms",
        )}},
    }


@pytest.mark.parametrize("price", [_PRICE_FLOOR_MICRODOLLARS_PER_M, 52_750, 1_055_000])
@pytest.mark.parametrize("tokens", [0, 1, 100, 1_000_000])
@pytest.mark.parametrize("state", ["open", "empty", "key-limit", "daily-limit", "spent"])
@pytest.mark.parametrize("terminal", ["settle", "refund", "frozen", "output"])
def test_rerank_embedding_money_differential(monkeypatch, price, tokens, state, terminal):
    models(monkeypatch, price)
    observed = []
    for model, route in ((EMBED, "embeddings"), (RERANK, "rerank")):
        store, workspace, key = seed(state)
        try:
            data = authorize(key, model, route, tokens)
        except HTTPException as exc:
            assert state != "open"
            observed.append((exc.status_code, exc.detail, asdict(store.credit_money[workspace.id])))
            continue
        hold = asdict(store.credit_money[workspace.id])
        auth = store.get_gateway_authorization(data["authorization_id"])
        assert auth is not None
        assert data["estimated_cost_microdollars"] == (max(1, (price * tokens + 500_000) // 1_000_000) if tokens else 0)
        replay = authorize(key, model, route, tokens)
        assert replay["authorization_id"] == auth.id and replay["idempotent_replay"]
        assert asdict(store.credit_money[workspace.id]) == hold
        assert auth.pricing_snapshot is None
        if terminal == "frozen":
            auth.stage_d_reason = "ok"
            auth.pricing_snapshot = canonical_pricing_snapshot(endpoint_pricing_document(
                (MODEL_ENDPOINTS[data["endpoint_id"]],),
            ))
        result = gateway._settle_gateway_authorization(GatewaySettleRequest(
            authorization_id=auth.id, selected_endpoint=data["endpoint_id"],
            actual_input_tokens=tokens, actual_output_tokens=123 if terminal == "output" else 0,
            route_type=route, request_id="fixture", elapsed_seconds=0.2,
        ), success=terminal != "refund", settings=Settings(environment="test"))["data"]
        expected = 0 if terminal == "refund" else (max(1, (price * tokens + 500_000) // 1_000_000) if tokens else 0)
        assert result["cost_microdollars"] == expected
        generation = store.get_generation(result["generation_id"])
        if terminal == "refund":
            assert generation is None
        else:
            assert generation is not None and generation.route_type == route and generation.model == model
            assert generation.tokens_prompt == tokens
            assert generation.tokens_completion == (123 if terminal == "output" else 0)
            samples = store.provider_benchmark_samples(model=model)
            assert len(samples) == 1 and samples[0].route_type == route
            assert samples[0].total_cost_microdollars == expected
            events = store.activity_events(workspace.id)
            assert len(events) == 1 and events[0]["model"] == model
            assert events[0]["cost_microdollars"] == expected
        money = asdict(store.credit_money[workspace.id])
        assert money["reserved_microdollars"] == 0
        assert money["total_usage_microdollars"] == expected
        refreshed = store.get_key_by_hash(key.hash)
        observed.append((hold, money, refreshed.usage_microdollars, refreshed.reserved_microdollars,
                         [(m.kind, m.amount_microdollars) for m in store.credit_movements.values()],
                         generation.total_cost_microdollars if generation else None))
    assert observed[0] == observed[1]


@pytest.mark.parametrize("route", [None, "chat.completions", "responses", "messages", "embeddings",
                                   "decide", "images", "videos", "audio.speech", "audio.transcriptions", "other"])
def test_rerank_model_refused_on_other_routes(monkeypatch, route):
    models(monkeypatch)
    _, _, key = seed()
    with pytest.raises(HTTPException) as caught:
        authorize(key, RERANK, route)
    assert caught.value.status_code == 400
    assert caught.value.detail == error(f"Model does not support chat completions: {RERANK}")


@pytest.mark.parametrize("model", [CHAT, EMBED, "voyage/missing"])
def test_other_models_refused_on_enabled_rerank(monkeypatch, model):
    models(monkeypatch)
    _, _, key = seed()
    with pytest.raises(HTTPException) as caught:
        authorize(key, model, "rerank")
    assert caught.value.status_code == 400
    assert caught.value.detail == error(f"Model does not support rerank: {model}")


def test_provider_flag_checked_again_at_authorize(monkeypatch):
    models(monkeypatch)
    monkeypatch.setitem(PROVIDERS, "voyage", replace(PROVIDERS["voyage"], supports_rerank=False))
    _, _, key = seed()
    with pytest.raises(HTTPException) as caught:
        authorize(key, RERANK, "rerank")
    assert caught.value.detail == error(f"Model does not support rerank: {RERANK}")


@pytest.mark.parametrize("model", ["voyage/rerank-2", "cohere/rerank-5-fast", "cohere/rerank-5-pro", "absent/model"])
def test_disabled_rerank_retains_baseline_refusal(model):
    assert all(not p.supports_rerank for p in PROVIDERS.values())
    _, _, key = seed()
    with pytest.raises(HTTPException) as caught:
        authorize(key, model, "rerank")
    assert caught.value.status_code == 400
    assert caught.value.detail == error(f"Model does not support chat completions: {model}")


@pytest.mark.parametrize("model,route", [("anthropic/claude-haiku-4.5", "rerank"),
    ("anthropic/claude-haiku-4.5", "embeddings"), ("openai/text-embedding-3-large", "chat.completions")])
def test_legacy_cross_route_acceptance_is_unchanged(model, route):
    _, _, key = seed()
    assert authorize(key, model, route)["model"] == model


@pytest.mark.parametrize("streamed", [False, True])
def test_rerank_stays_outside_async_and_tickets(streamed):
    for route in ("embeddings", "rerank"):
        context = billing_snapshot.Eligibility(route_type=route, streamed=streamed)
        assert billing_snapshot.exclusion(context) == "unsupported_route"
        with pytest.raises(ValueError, match="unsupported_route"):
            billing_snapshot.require_eligible(context)
        claims = json.loads((Path(__file__).parent / "fixtures/async_settlement/authorize_v1.json").read_text())["claims"]
        with pytest.raises(ValidationError, match="route_type"):
            TicketClaims.model_validate({**claims, "route_type": route, "streamed": streamed})


def test_rerank_candidates_are_input_only_and_private(monkeypatch):
    endpoints = models(monkeypatch)
    assert routing.rerank_route_endpoint_candidates({"model": RERANK}, Settings(environment="test")) == [
        (MODELS[RERANK], endpoints[RERANK])]
    for resolver in (routing.chat_route_endpoint_candidates, routing.embeddings_route_endpoint_candidates):
        with pytest.raises(HTTPException):
            resolver({"model": RERANK}, Settings(environment="test"))


def test_rerank_async_projection_cannot_issue_a_ticket():
    from tests.test_async_settle_ticket import CLAIMS, authorization, endpoint, runtime, settings
    from trusted_router.services.async_settle import projection

    for route in ("embeddings", "rerank"):
        assert projection(
            authorization=authorization(), endpoints=[endpoint()],
            requested=billing_snapshot.Eligibility(route_type=route), runtime=runtime(),
            settings=settings(async_settle_admission_enabled=True), now=CLAIMS["iat"] + 60,
        ) == {"async_eligible": False}


@pytest.mark.parametrize("price", [10_000, 52_750, 1_055_000])
@pytest.mark.parametrize("tokens", [0, 1, 100, 1_000_000])
@pytest.mark.parametrize("tier", [0, 1, 2, 3])
@pytest.mark.parametrize("admitted", [True, False])
def test_rerank_embedding_lease_differential(monkeypatch, price, tokens, tier, admitted):
    from tests.test_fastpath_frontdoor import _Door, _transport
    from tests.test_fastpath_pilot_route import ADMIT, WORKSPACE, _pilot, _request, _seed
    from trusted_router.services import fastpath_frontdoor as fd
    from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE

    models(monkeypatch, price)
    monkeypatch.setattr(gateway, "verify_boot_auth", lambda **_kwargs: True)
    observed = []
    for model, route in ((EMBED, "embeddings"), (RERANK, "rerank")):
        store, database, key = _seed()
        row = database.typed[CREDIT_BALANCE_TABLE][(WORKSPACE, 0)]
        row["trust_tier"] = tier
        door = _Door({"/v1/authorize": ADMIT if admitted else {"Status": "unavailable"}})
        client = fd.FrontDoorClient(lambda _budget: ("10.0.0.1:8080",), timeout_seconds=1.5,
                                   clock=lambda: 0.0, transport=_transport({"10.0.0.1:8080": door}))
        monkeypatch.setattr(gateway, "_fastpath_client", client)
        data = gateway._authorize_gateway_sync(_request(), GatewayAuthorizeRequest(
            api_key_hash=key.hash, model=model, route_type=route,
            estimated_input_tokens=tokens, max_output_tokens=1, stream=False,
        ), _pilot())["data"]
        auth = store.get_gateway_authorization(data["authorization_id"])
        assert auth is not None
        assert door.seen and door.seen[0][0] == "/v1/authorize"
        assert door.seen[0][1]["Estimate"] == auth.estimated_microdollars
        reservation = database.reservations[auth.credit_reservation_id]
        observed.append((auth.estimated_microdollars, auth.fastpath_envelope,
                         reservation["credit_reserved_micro"], reservation["key_reserved_micro"],
                         door.seen[0][1]["Estimate"]))
    assert observed[0] == observed[1]


@pytest.mark.parametrize("backend", ["memory", "typed"])
def test_disabled_rerank_matches_frozen_authorize_bytes(monkeypatch, caplog, backend):
    from tests.test_authorize_candidates_differential import _compare, _model_spellings

    assert all(not provider.supports_rerank for provider in PROVIDERS.values())
    cases = [{"model": model, "route_type": "rerank", "max_output_tokens": 1,
              "estimated_input_tokens": tokens}
             for model in [*_model_spellings(), "voyage/rerank-2", "cohere/rerank-5-fast",
                           "cohere/rerank-5-pro", "absent/model"]
             for tokens in (0, 100)]
    counts = _compare(cases, backend, monkeypatch, caplog)
    assert counts[200] > 0 and counts[400] > 0


@pytest.mark.parametrize("price,tokens", [(10_000, 0), (10_000, 1), (52_750, 100), (1_055_000, 1_000_000)])
@pytest.mark.parametrize("success", [False, True])
@pytest.mark.parametrize("outbox", [False, True])
def test_rerank_typed_settlement_conformance(monkeypatch, price, tokens, success, outbox):
    from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
    from tests.test_settle_outbox_drain import _typed_credit, _typed_key

    models(monkeypatch, price)
    outcomes = []
    for model, route in ((EMBED, "embeddings"), (RERANK, "rerank")):
        store, database, key = _seed_typed_gateway_store(generation_records_enabled=True,
                                                       analytics_outbox_enabled=True)
        data = authorize(key, model, route, tokens)
        body = GatewaySettleRequest(
            authorization_id=data["authorization_id"], selected_endpoint=data["endpoint_id"],
            route_type=route, actual_input_tokens=tokens, actual_output_tokens=0,
            elapsed_seconds=0.2, request_id="typed-fixture",
        )
        settings = Settings(environment="test", settle_outbox_enabled=outbox)
        result = gateway._settle_gateway_authorization(body, success=success, settings=settings)["data"]
        expected = max(1, (price * tokens + 500_000) // 1_000_000) if success and tokens else 0
        assert result["cost_microdollars"] == expected
        assert _typed_credit(database, key.workspace_id)["total_usage"] == expected
        assert _typed_key(database, key.hash)["usage"] == expected
        assert _typed_credit(database, key.workspace_id)["reserved"] == 0
        auth = store.get_gateway_authorization(data["authorization_id"])
        assert auth is not None and auth.settled
        assert database.reservations[auth.credit_reservation_id]["actual_micro"] == expected
        if success:
            gen = store.get_generation(result["generation_id"])
            assert gen is not None and gen.route_type == route and gen.model == model
        again = gateway._settle_gateway_authorization(body, success=success, settings=settings)["data"]
        assert again["cost_microdollars"] == expected
        assert _typed_credit(database, key.workspace_id)["total_usage"] == expected
        outcomes.append((result["cost_microdollars"], auth.estimated_microdollars,
                         _typed_key(database, key.hash)["usage"]))
    assert outcomes[0] == outcomes[1]


@pytest.mark.parametrize("estimate,actual", [(0, 1), (100, 1), (100, 1_000), (1_000_000, 333)])
@pytest.mark.parametrize("price", [10_000, 52_750])
def test_rerank_estimated_and_actual_usage_differential(monkeypatch, estimate, actual, price):
    models(monkeypatch, price)
    observed = []
    for model, route in ((EMBED, "embeddings"), (RERANK, "rerank")):
        store, workspace, key = seed()
        data = authorize(key, model, route, estimate)
        hold = asdict(store.credit_money[workspace.id])
        result = gateway._settle_gateway_authorization(GatewaySettleRequest(
            authorization_id=data["authorization_id"], selected_endpoint=data["endpoint_id"],
            actual_input_tokens=actual, actual_output_tokens=0, route_type=route,
        ), success=True, settings=Settings(environment="test"))["data"]
        expected = max(1, (price * actual + 500_000) // 1_000_000)
        assert result["cost_microdollars"] == expected
        after = asdict(store.credit_money[workspace.id])
        assert after["reserved_microdollars"] == 0
        assert after["total_usage_microdollars"] == expected
        observed.append((hold, after))
    assert observed[0] == observed[1]


def test_rerank_has_no_live_routing_and_rejects_reused_request_identity(monkeypatch):
    from trusted_router.authorize_candidates import CandidateRequest, plan_route

    models(monkeypatch)
    plan = plan_route(CandidateRequest(route_type="rerank", route_model_id=RERANK,
                                      routing_body={"model": RERANK}, body_region=None,
                                      video_resolution=None, video_allowed_providers=None),
                      Settings(environment="test"), region="us-central1")
    assert plan.is_rerank and not plan.supports_live_routing
    _, _, key = seed()
    authorize(key, RERANK, "rerank", 100)
    with pytest.raises(HTTPException) as caught:
        authorize(key, RERANK, "rerank", 101)
    assert caught.value.status_code == 409
