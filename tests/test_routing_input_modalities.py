from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tests.test_gateway_fallback_billing import _client_and_key
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS
from trusted_router.catalog_data import Model, ModelEndpoint, Provider
from trusted_router.config import Settings
from trusted_router.routes.internal import gateway
from trusted_router.routing import chat_route_endpoint_candidates, provider_route_preferences
from trusted_router.schemas import GatewayAuthorizeRequest


@pytest.fixture
def vision_routes(monkeypatch):
    model = Model(id="fixture/vision", name="Vision", provider="vision-test", context_length=8192,
                  input_modalities=("text", "image"))
    monkeypatch.setitem(MODELS, model.id, model)
    monkeypatch.setitem(PROVIDERS, "vision-test", Provider(slug="vision-test", name="Vision", supports_prepaid=True))
    endpoints = [ModelEndpoint(id=f"{model.id}@vision-test/{name}", model_id=model.id,
                               provider="vision-test", usage_type="Credits", input_modalities=modalities)
                 for name, modalities in [("text", ("text",)), ("unknown", None), ("image", ("text", "image"))]]
    for endpoint in endpoints:
        monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)
    return model, endpoints


@pytest.mark.parametrize("allow_fallbacks", [True, False])
def test_authorize_schema_retains_modalities_and_filters_all_candidates(vision_routes, allow_fallbacks):
    model, endpoints = vision_routes
    body = GatewayAuthorizeRequest(api_key_hash="fixture", model=model.id,
                                   input_modalities=["text", "image"],
                                   provider={"allow_fallbacks": allow_fallbacks})
    candidates = chat_route_endpoint_candidates(body.model_dump(exclude_none=True), Settings(environment="test"))
    assert [endpoint.id for _, endpoint in candidates] == [endpoints[2].id]


def test_text_requests_keep_existing_routes(vision_routes):
    model, endpoints = vision_routes
    candidates = chat_route_endpoint_candidates({"model": model.id}, Settings(environment="test"))
    assert {endpoint.id for _, endpoint in candidates} == {endpoint.id for endpoint in endpoints}


def test_confidential_floor_cannot_fall_back_to_plain_vision(vision_routes, monkeypatch):
    model, endpoints = vision_routes
    private = Provider(slug="private-vision-test", name="Private", supports_prepaid=True,
                       stores_content=False, provider_zero_data_retention=True,
                       provider_confidential_compute=True, provider_e2ee=True)
    monkeypatch.setitem(PROVIDERS, private.slug, private)
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoints[0].id, replace(endpoints[0], provider=private.slug))
    settings = Settings(environment="test")
    body = {"model": model.id, "provider": {"min_privacy": "confidential"}}
    assert chat_route_endpoint_candidates(body, settings)[0][1].provider == private.slug
    with pytest.raises(HTTPException) as error:
        chat_route_endpoint_candidates({**body, "input_modalities": ["image"]}, settings)
    assert error.value.status_code == 400


@pytest.mark.parametrize("provider", [{}, {"data_collection": "deny"}, {"min_privacy": "confidential"}])
def test_missing_vision_route_fails_closed(vision_routes, monkeypatch, provider):
    model, endpoints = vision_routes
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoints[2].id, replace(endpoints[2], input_modalities=("text",)))
    with pytest.raises(HTTPException) as error:
        chat_route_endpoint_candidates({"model": model.id, "input_modalities": ["image"], "provider": provider},
                                       Settings(environment="test"))
    assert error.value.status_code == 400


def test_replay_cannot_restore_text_only_route(vision_routes):
    model, endpoints = vision_routes
    authorization = SimpleNamespace(user_provided_model_id=None, candidate_endpoint_ids=[e.id for e in endpoints], endpoint_id=endpoints[0].id)
    candidates = gateway._authorization_endpoint_candidates(authorization, [], input_modalities=frozenset({"image"}))
    assert candidates == [(model, endpoints[2])]
    authorization.candidate_endpoint_ids = [endpoints[0].id]
    with pytest.raises(HTTPException) as error:
        gateway._authorization_endpoint_candidates(authorization, candidates, input_modalities=frozenset({"image"}))
    assert error.value.status_code == 409


@pytest.mark.parametrize("value", ["image", ["imag"], [False], [None]])
def test_invalid_modality_rejected(value):
    with pytest.raises(HTTPException):
        provider_route_preferences({"input_modalities": value})


@pytest.mark.parametrize("route_type", ["chat.completions", "responses"])
def test_gateway_image_authorization_and_replay(vision_routes, route_type):
    model, endpoints = vision_routes
    client, key = _client_and_key()
    body = {"model": model.id, "api_key_hash": key["hash"],
            "input_modalities": ["text", "image"], "route_type": route_type,
            "estimated_input_tokens": 10, "max_output_tokens": 10,
            "idempotency_key": f"vision-{route_type}"}
    for replay in (False, True):
        response = client.post("/v1/internal/gateway/authorize", json=body)
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["idempotent_replay"] is replay
        assert [row["endpoint_id"] for row in data["route_candidates"]] == [endpoints[2].id]
