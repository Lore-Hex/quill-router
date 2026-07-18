from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from typing import Any

from fastapi.testclient import TestClient

from trusted_router.catalog import (
    AUTO_MODEL_ID,
    CHEAP_MODEL_ID,
    E2E_MODEL_ID,
    MODELS,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_ZERO_RETENTION,
    ROUTING_MODEL_MIN_PRIVACY_TIERS,
    SYNTH_MODEL_ID,
    ZDR_MODEL_ID,
    endpoint_privacy_tier,
)
from trusted_router.choose_catalog import build_choose_catalog_payload
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates


def _walk(value: Any) -> Iterator[tuple[str, Any]]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield str(key), child
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def test_choose_catalog_is_compact_endpoint_scoped_and_cached(client: TestClient) -> None:
    response = client.get("/choose/catalog.json")

    assert response.status_code == 200
    assert response.headers["x-tr-cache"] == "bypass"
    assert response.headers["cache-control"] == (
        "public, max-age=15, s-maxage=300, stale-while-revalidate=86400"
    )
    assert len(response.content) < 150_000

    payload = response.json()
    assert payload["catalog_model_count"] > payload["evaluated_model_count"] > 0
    assert payload["catalog_route_count"] > payload["catalog_model_count"]
    for model in payload["models"]:
        assert model["quality"]["score"] > 0
        assert model["endpoints"]
        for endpoint in model["endpoints"]:
            assert isinstance(
                endpoint["prompt_price_microdollars_per_million_tokens"], int
            )
            assert isinstance(
                endpoint["completion_price_microdollars_per_million_tokens"], int
            )
            assert endpoint["provider"]
            assert endpoint["usage_type"] in {"BYOK", "Credits"}
            assert endpoint["privacy_tier"] in {0, 1, 2, 3}

    forbidden_keys = {
        "api_key",
        "authorization",
        "byok_key",
        "prompt",
        "output",
        "secret",
        "workspace_id",
    }
    assert not ({key.lower() for key, _value in _walk(payload)} & forbidden_keys)


def test_choose_catalog_never_inherits_privacy_from_another_provider(
    client: TestClient,
) -> None:
    payload = client.get("/choose/catalog.json").json()
    gmi_endpoints = [
        endpoint
        for model in payload["models"]
        for endpoint in model["endpoints"]
        if endpoint["provider"] == "gmi"
    ]

    assert gmi_endpoints
    assert {endpoint["privacy_tier"] for endpoint in gmi_endpoints} == {0}
    assert all(endpoint["privacy_tier_label"] == "Standard" for endpoint in gmi_endpoints)

    deepseek = next(
        model for model in payload["models"] if model["id"] == "deepseek/deepseek-v4-pro"
    )
    assert any(endpoint["privacy_tier"] == 2 for endpoint in deepseek["endpoints"])
    assert all(endpoint["privacy_tier"] < 3 for endpoint in deepseek["endpoints"])


def test_choose_route_guarantees_match_runtime_source_of_truth(
    client: TestClient,
    test_settings: Settings,
) -> None:
    routes = {
        route["id"]: route for route in client.get("/choose/catalog.json").json()["routes"]
    }

    assert routes[AUTO_MODEL_ID]["min_privacy_tier"] == 0
    assert routes[CHEAP_MODEL_ID]["min_privacy_tier"] == 0
    assert routes[ZDR_MODEL_ID]["min_privacy_tier"] == PRIVACY_TIER_ZERO_RETENTION
    assert routes[E2E_MODEL_ID]["min_privacy_tier"] == PRIVACY_TIER_CONFIDENTIAL
    assert ROUTING_MODEL_MIN_PRIVACY_TIERS[ZDR_MODEL_ID] == PRIVACY_TIER_ZERO_RETENTION
    assert ROUTING_MODEL_MIN_PRIVACY_TIERS[E2E_MODEL_ID] == PRIVACY_TIER_CONFIDENTIAL
    assert AUTO_MODEL_ID not in ROUTING_MODEL_MIN_PRIVACY_TIERS
    assert CHEAP_MODEL_ID not in ROUTING_MODEL_MIN_PRIVACY_TIERS

    zdr_candidates = chat_route_endpoint_candidates({"model": ZDR_MODEL_ID}, test_settings)
    e2e_candidates = chat_route_endpoint_candidates({"model": E2E_MODEL_ID}, test_settings)
    assert zdr_candidates
    assert e2e_candidates
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_ZERO_RETENTION
        for _model, endpoint in zdr_candidates
    )
    assert all(
        endpoint_privacy_tier(endpoint) >= PRIVACY_TIER_CONFIDENTIAL
        for _model, endpoint in e2e_candidates
    )
    assert all(endpoint.provider != "gmi" for _model, endpoint in e2e_candidates)


def test_choose_synth_pricing_is_component_usage_not_free(client: TestClient) -> None:
    routes = {
        route["id"]: route for route in client.get("/choose/catalog.json").json()["routes"]
    }
    synth = routes[SYNTH_MODEL_ID]

    assert synth["pricing_mode"] == "component_usage"
    assert synth["prompt_price_min_microdollars_per_million_tokens"] is None
    assert synth["completion_price_min_microdollars_per_million_tokens"] is None
    assert "Every inner inference call is billable" in synth["description"]


def test_choose_catalog_omits_unscored_and_zero_score_models() -> None:
    model = MODELS["openai/gpt-5.5"]
    payload = build_choose_catalog_payload(
        catalog_models=[model],
        quality_models={model.id: {"rank": 1}},
        quality_updated_at="2026-07-18T00:00:00Z",
        measured={"models": [], "generated_at": "2026-07-18T00:00:00Z"},
    )

    assert payload["catalog_model_count"] == 1
    assert payload["catalog_route_count"] > 0
    assert payload["evaluated_model_count"] == 0
    assert payload["models"] == []


def test_choose_page_and_client_do_not_repeat_old_false_claims(client: TestClient) -> None:
    page = client.get("/choose").text
    script = client.get("/static/choose-app.js").text
    combined = f"{page}\n{script}".lower()

    assert "best fit, zero-retention" not in combined
    assert "cheapest capable, tee" not in combined
    assert "every model trustedrouter can reach plotted" not in combined
    assert "tps≈" not in combined
    assert 'const catalog_url = "/choose/catalog.json"' in combined
    assert "/v1/models" not in script
    assert "/ai-iq/models.json" not in script
    assert json.dumps(client.get("/choose/catalog.json").json()) not in page
