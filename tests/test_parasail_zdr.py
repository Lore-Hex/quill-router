from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    PRIVACY_TIER_ZERO_RETENTION,
    PROVIDERS,
    ZDR_MODEL_ID,
    endpoint_privacy_tier,
    endpoint_zero_data_retention,
    endpoints_for_model,
    provider_privacy_tier,
)
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates
from trusted_router.routing_candidates import zdr_candidate_models


def _serve_on_parasail(monkeypatch: pytest.MonkeyPatch, model_id: str) -> None:
    """Parasail serves the model on a fixture route: its ZDR posture is a rule
    of Parasail's routes, whatever Parasail lists today."""
    route = ModelEndpoint(
        id=f"{model_id}@parasail/prepaid", model_id=model_id,
        provider="parasail", usage_type="Credits", upstream_id=model_id,
    )
    monkeypatch.setitem(MODEL_ENDPOINTS, route.id, route)


def test_parasail_serverless_and_dedicated_routes_are_zdr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_on_parasail(monkeypatch, "z-ai/glm-5.2")
    provider = PROVIDERS["parasail"]
    endpoints = [
        endpoint
        for endpoint in endpoints_for_model("z-ai/glm-5.2")
        if endpoint.provider == "parasail"
    ]

    assert endpoints
    assert provider.stores_content is False
    assert provider.provider_zero_data_retention is True
    assert provider_privacy_tier(provider) >= PRIVACY_TIER_ZERO_RETENTION
    assert all(endpoint_zero_data_retention(endpoint) is True for endpoint in endpoints)
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION
        for endpoint in endpoints
    )
    assert "serverless and dedicated" in provider.provider_policy
    assert "batch" in provider.provider_policy


def test_parasail_can_satisfy_direct_and_alias_zdr_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve_on_parasail(monkeypatch, "z-ai/glm-5.2")
    # And one model of the ZDR alias's ladder, whichever models it holds today.
    _serve_on_parasail(monkeypatch, zdr_candidate_models()[0].id)
    settings = Settings(environment="test")
    direct = chat_route_endpoint_candidates(
        {
            "model": "z-ai/glm-5.2",
            "provider": {"only": ["parasail"], "min_privacy": "zdr"},
        },
        settings,
    )
    alias = chat_route_endpoint_candidates(
        {"model": ZDR_MODEL_ID, "provider": {"only": ["parasail"]}},
        settings,
    )

    assert direct
    assert alias
    assert all(endpoint.provider == "parasail" for _model, endpoint in direct)
    assert all(endpoint.provider == "parasail" for _model, endpoint in alias)


@pytest.mark.provider_health
def test_parasail_serves_glm_5_2() -> None:
    # Live provider state: provider-catalog-health.yml reports it hourly, and
    # the price refresh does not wait on it.
    assert any(endpoint.provider == "parasail" for endpoint in endpoints_for_model("z-ai/glm-5.2"))


def test_parasail_zdr_is_published_with_policy_source(client: TestClient) -> None:
    providers = {
        item["id"]: item for item in client.get("/v1/providers").json()["data"]
    }
    parasail = providers["parasail"]

    assert parasail["provider_zero_data_retention"] is True
    assert parasail["stores_content"] is False
    assert parasail["zero_data_retention_scope"] == "provider"
    assert parasail["provider_policy_url"] == (
        "https://docs.parasail.io/parasail-docs/security-and-account-management/"
        "data-privacy-retention"
    )

    zdr = client.get("/v1/endpoints/zdr").json()["data"]
    assert "parasail" in {item["provider"] for item in zdr}
