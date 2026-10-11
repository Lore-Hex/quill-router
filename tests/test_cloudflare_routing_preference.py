"""Operator-funded capacity affects default prepaid ranking, not eligibility."""

from dataclasses import replace
from typing import Any

import pytest

from tests.fixture_routes import drop_routes, serve_on_fixture_route
from tests.test_gateway_fallback_billing import _client_and_key
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.config import Settings
from trusted_router.routing import (
    _MODEL_PROVIDER_PREFERENCE,
    _sort_endpoint_candidates,
    chat_route_endpoint_candidates,
    provider_route_preferences,
)

CLOUDFLARE = "cloudflare-workers-ai"
MODEL = "unit/cloudflare-preference"


@pytest.fixture
def routes(monkeypatch: pytest.MonkeyPatch) -> list[ModelEndpoint]:
    # Verify policy precedence independently of the changing measured snapshot.
    monkeypatch.setattr(
        "trusted_router.routing.measured_provider_rank",
        lambda provider, sort: {"deepinfra": 0, CLOUDFLARE: 1, "parasail": 4}.get(provider, 1000),
    )
    return [
        serve_on_fixture_route(
            monkeypatch, MODEL, provider, author="openai",
            prompt_price_microdollars_per_million_tokens=price,
            input_modalities=("text", "image") if provider == "deepinfra" else ("text",),
            supported_parameters=("tools",) if provider == "deepinfra" else (),
        )
        for provider, price in (("deepinfra", 100_000), ("parasail", 200_000), (CLOUDFLARE, 300_000))
    ]


def _providers(body: dict[str, Any]) -> list[str]:
    return [
        endpoint.provider
        for _, endpoint in chat_route_endpoint_candidates(
            {"model": MODEL, **body}, Settings(environment="test"),
        )
    ]


def test_default_prepaid_prefers_cloudflare_without_changing_prices_or_fallbacks(
    routes: list[ModelEndpoint],
) -> None:
    candidates = chat_route_endpoint_candidates({"model": MODEL}, Settings(environment="test"))
    assert [endpoint.provider for _, endpoint in candidates] == [CLOUDFLARE, "deepinfra", "parasail"]
    assert {endpoint.id for _, endpoint in candidates} == {endpoint.id for endpoint in routes}
    for _, endpoint in candidates:
        assert endpoint is next(route for route in routes if route.id == endpoint.id)


@pytest.mark.parametrize("sort", ["price", "latency", "throughput"])
def test_explicit_sort_overrides_cloudflare_preference(routes: list[ModelEndpoint], sort: str) -> None:
    assert _providers({"provider": {"sort": sort}})[0] == "deepinfra"


@pytest.mark.parametrize(
    ("preferences", "expected"),
    [
        ({"order": ["parasail"]}, ["parasail", CLOUDFLARE, "deepinfra"]),
        ({"order": ["deepinfra", "parasail"]}, ["deepinfra", "parasail", CLOUDFLARE]),
        ({"only": ["deepinfra"]}, ["deepinfra"]),
        ({"ignore": [CLOUDFLARE]}, ["deepinfra", "parasail"]),
        ({"allow_fallbacks": False}, [CLOUDFLARE]),
    ],
)
def test_caller_provider_controls_preserved(
    routes: list[ModelEndpoint], preferences: dict[str, Any], expected: list[str],
) -> None:
    assert _providers({"provider": preferences}) == expected


def test_byok_cloudflare_is_not_promoted(routes: list[ModelEndpoint]) -> None:
    model = MODELS[MODEL]
    candidates = [(model, replace(route, usage_type="BYOK")) for route in routes]
    ordered = _sort_endpoint_candidates(candidates, provider_route_preferences({}))
    assert [endpoint.provider for _, endpoint in ordered] == ["deepinfra", CLOUDFLARE, "parasail"]


def test_mixed_routes_promote_only_prepaid_cloudflare(routes: list[ModelEndpoint]) -> None:
    model = MODELS[MODEL]
    byok = replace(routes[-1], id=f"{MODEL}@{CLOUDFLARE}/byok", usage_type="BYOK")
    candidates = [(model, route) for route in (routes[0], byok, routes[-1])]
    ordered = _sort_endpoint_candidates(candidates, provider_route_preferences({}))
    assert [endpoint.id for _, endpoint in ordered] == [routes[-1].id, routes[0].id, byok.id]


def test_cloudflare_does_not_replace_requested_primary_model(
    routes: list[ModelEndpoint], monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = "unit/primary-not-on-cloudflare"
    serve_on_fixture_route(monkeypatch, primary, "parasail", author="openai")
    candidates = chat_route_endpoint_candidates(
        {"model": primary, "models": [MODEL]}, Settings(environment="test"),
    )
    assert [(model.id, endpoint.provider) for model, endpoint in candidates] == [
        (primary, "parasail"), (MODEL, CLOUDFLARE), (MODEL, "deepinfra"), (MODEL, "parasail"),
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"input_modalities": ["text", "image"]},
        {"requested_parameters": ["tools"], "provider": {"require_parameters": True}},
        {"provider": {"max_price": {"prompt": "0.15"}}},
    ],
)
def test_cloudflare_preference_cannot_bypass_eligibility(
    routes: list[ModelEndpoint], body: dict[str, Any],
) -> None:
    assert _providers(body) == ["deepinfra"]


def test_confidential_floor_excludes_cloudflare(
    routes: list[ModelEndpoint], monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_on_fixture_route(monkeypatch, MODEL, "tinfoil", author="openai")
    assert _providers({"provider": {"min_privacy": "confidential"}}) == ["tinfoil"]


def test_expired_cloudflare_route_is_not_preferred(
    routes: list[ModelEndpoint], monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    route = routes[-1]
    monkeypatch.setitem(
        MODEL_ENDPOINTS, route.id, replace(route, catalog_valid_until=datetime(2000, 1, 1, tzinfo=UTC)),
    )
    assert _providers({}) == ["deepinfra", "parasail"]


def test_model_incident_demotion_overrides_funded_preference(
    routes: list[ModelEndpoint], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(_MODEL_PROVIDER_PREFERENCE, MODEL, {CLOUDFLARE: 10})
    assert _providers({}) == ["deepinfra", "parasail", CLOUDFLARE]


def test_cloudflare_precedes_glm_52_normal_provider_bonus(monkeypatch: pytest.MonkeyPatch) -> None:
    model_id = "z-ai/glm-5.2"
    drop_routes(monkeypatch, model_id)
    for provider in ("parasail", CLOUDFLARE):
        serve_on_fixture_route(monkeypatch, model_id, provider, author="zai")
    assert _providers({"model": model_id}) == [CLOUDFLARE, "parasail"]


@pytest.mark.parametrize("route_type", ["chat.completions", "responses"])
def test_gateway_authorization_prefers_cloudflare_and_retains_fallbacks(
    routes: list[ModelEndpoint], route_type: str,
) -> None:
    client, key = _client_and_key()
    with client:
        response = client.post(
            "/v1/internal/gateway/authorize",
            json={
                "api_key_hash": key["hash"], "model": MODEL,
                "estimated_input_tokens": 10, "max_output_tokens": 10,
                "route_type": route_type, "idempotency_key": f"cloudflare-{route_type}",
            },
        )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["provider"] == CLOUDFLARE
    assert data["usage_type"] == "Credits"
    assert [row["provider"] for row in data["route_candidates"]] == [CLOUDFLARE, "deepinfra", "parasail"]
