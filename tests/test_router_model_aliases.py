from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.catalog import MODELS, model_to_openrouter_shape
from trusted_router.config import Settings
from trusted_router.model_aliases import canonical_router_model_id
from trusted_router.routing import canonical_model_id, normalize_routing_inputs
from trusted_router.schemas import GatewayAuthorizeRequest

AUTO_ALIASES = ("trustedrouter/auto", "trustedrouter/auto-routing", "nyte/auto", "nyte/auto-routing")


@pytest.mark.parametrize("alias", AUTO_ALIASES)
def test_auto_aliases_have_identical_routing(alias: str) -> None:
    for variant in ("", ":nitro", ":floor"):
        assert canonical_model_id(alias + variant) == "trustedrouter/auto"
        for provider in ({}, {"usage": "credits", "only": ["openai"], "allow_fallbacks": False}):
            canonical = normalize_routing_inputs({"model": "trustedrouter/auto" + variant, "provider": provider}, Settings())
            assert normalize_routing_inputs({"model": alias + variant, "provider": provider}, Settings()) == canonical


@pytest.mark.parametrize("model", [mid for mid in MODELS if mid.startswith("trustedrouter/")])
def test_every_router_model_has_a_nyte_alias(model: str) -> None:
    alias = model.replace("trustedrouter/", "nyte/", 1)
    assert canonical_model_id(alias) == canonical_model_id(model)
    assert alias in model_to_openrouter_shape(MODELS[model])["trustedrouter"]["aliases"]


def test_namespace_does_not_turn_unknown_models_into_auto() -> None:
    assert canonical_model_id("nyte/not-a-real-model") == "trustedrouter/not-a-real-model"
    assert canonical_model_id("nyte/auto-routing:unknown") == "trustedrouter/auto:unknown"
    for model in ("openai/auto-routing", "notnyte/auto", "nyteevil/auto", "x-ai/grok-4.7"):
        assert canonical_router_model_id(model) == model


def test_gateway_normalizes_before_policy_and_idempotency() -> None:
    for alias in AUTO_ALIASES:
        request = GatewayAuthorizeRequest(api_key_hash="hash", model=alias, models=["nyte/auto-routing", "nyte/user-example"])
        assert request.model == "trustedrouter/auto"
        assert request.models == ["trustedrouter/auto", "trustedrouter/user-example"]


def test_aliases_share_gateway_authorization(client: TestClient, user_headers: dict[str, str]) -> None:
    key = client.post("/v1/keys", headers=user_headers, json={"name": "alias test"}).json()["data"]["hash"]
    auth_ids = set()
    for alias in AUTO_ALIASES:
        response = client.post("/v1/internal/gateway/authorize", json={
            "api_key_hash": key, "model": alias, "idempotency_key": "same-auto-request",
            "estimated_input_tokens": 1, "max_output_tokens": 1, "route_type": "chat.completions",
        })
        assert response.status_code == 200, response.text
        data = response.json()["data"]
        assert data["requested_model"] == "trustedrouter/auto"
        auth_ids.add(data["authorization_id"])
    assert len(auth_ids) == 1


def test_aliases_do_not_bypass_privacy_or_byok_restrictions(client: TestClient, user_headers: dict[str, str]) -> None:
    key = client.post("/v1/keys", headers=user_headers, json={"name": "alias restrictions"}).json()["data"]["hash"]
    for model, provider in (("nyte/auto-routing", {"usage": "byok"}), ("nyte/confidential", {"only": ["openai"]})):
        response = client.post("/v1/internal/gateway/authorize", json={
            "api_key_hash": key, "model": model, "estimated_input_tokens": 1, "max_output_tokens": 1,
            "route_type": "chat.completions", "provider": provider,
        })
        assert response.status_code == 400, response.text


@pytest.mark.parametrize("alias", AUTO_ALIASES[1:])
def test_alias_public_pages_redirect_without_catalog_duplicates(client: TestClient, alias: str) -> None:
    response = client.get(f"/models/{alias}", follow_redirects=False)
    assert response.status_code == 301
    assert response.headers["location"] == "/models/trustedrouter/auto"
    assert alias not in MODELS
    assert client.get(f"/v1/models/{alias}/endpoints").json() == client.get("/v1/models/trustedrouter/auto/endpoints").json()
