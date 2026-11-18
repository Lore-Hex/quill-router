"""Venice remains selectable while default prepaid traffic prefers other hosts."""

from dataclasses import replace
from typing import Any

import pytest

from tests.fixture_routes import drop_routes, serve_on_fixture_route
from tests.test_gateway_fallback_billing import _client_and_key
from trusted_router import routing_candidates
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.config import Settings
from trusted_router.routing import (
    _sort_endpoint_candidates,
    canonical_model_id,
    chat_route_endpoint_candidates,
    provider_route_preferences,
)

MODEL = "unit/venice-preference"


@pytest.fixture
def routes(monkeypatch: pytest.MonkeyPatch) -> list[ModelEndpoint]:
    # Venice wins on measured speed and price, so the policy must actually
    # override those defaults without defeating an explicit caller sort.
    monkeypatch.setattr(
        "trusted_router.routing.measured_provider_rank",
        lambda provider, sort: {"venice": 0, "parasail": 1}.get(provider, 1000),
    )
    monkeypatch.setattr(
        "trusted_router.routing.workload_ranks",
        lambda model, input_tokens, output_tokens: {
            sort: {"venice": 0, "parasail": 1}
            for sort in ("default", "latency", "throughput")
        },
    )
    return [
        serve_on_fixture_route(
            monkeypatch, MODEL, provider, author="openai",
            prompt_price_microdollars_per_million_tokens=price,
            input_modalities=("text", "image") if provider == "venice" else ("text",),
            supported_parameters=("tools",) if provider == "venice" else (),
        )
        for provider, price in (("venice", 100_000), ("parasail", 200_000), ("telnyx", 300_000))
    ]


def _providers(body: dict[str, Any]) -> list[str]:
    return [endpoint.provider for _, endpoint in chat_route_endpoint_candidates(
        {"model": MODEL, **body}, Settings(environment="test"),
    )]


def test_default_keeps_other_healthy_and_unknown_hosts_ahead_of_venice(
    routes: list[ModelEndpoint],
) -> None:
    candidates = chat_route_endpoint_candidates({"model": MODEL}, Settings(environment="test"))
    assert [endpoint.provider for _, endpoint in candidates] == ["parasail", "telnyx", "venice"]
    assert {endpoint.id for _, endpoint in candidates} == {endpoint.id for endpoint in routes}
    for _, endpoint in candidates:
        assert endpoint is next(route for route in routes if route.id == endpoint.id)


@pytest.mark.parametrize("sort", ["price", "latency", "throughput"])
def test_explicit_sort_preserved(routes: list[ModelEndpoint], sort: str) -> None:
    assert _providers({"provider": {"sort": sort}})[0] == "venice"


@pytest.mark.parametrize(
    ("preferences", "expected"),
    [
        ({"order": ["venice"]}, ["venice", "parasail", "telnyx"]),
        ({"order": ["telnyx", "venice"]}, ["telnyx", "venice", "parasail"]),
        ({"only": ["venice"]}, ["venice"]),
        ({"ignore": ["venice"]}, ["parasail", "telnyx"]),
        ({"allow_fallbacks": False}, ["parasail"]),
        ({"order": ["venice"], "allow_fallbacks": False}, ["venice"]),
    ],
)
def test_explicit_provider_controls_preserved(
    routes: list[ModelEndpoint], preferences: dict[str, Any], expected: list[str],
) -> None:
    assert _providers({"provider": preferences}) == expected


def test_byok_still_uses_measured_speed(routes: list[ModelEndpoint]) -> None:
    candidates = [(MODELS[MODEL], replace(route, usage_type="BYOK")) for route in routes]
    ordered = _sort_endpoint_candidates(candidates, provider_route_preferences({}))
    assert ordered[0][1].provider == "venice"


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        ({"venice": 0, "parasail": 2001, "telnyx": 2002}, ["venice", "parasail", "telnyx"]),
        ({"venice": 2002, "parasail": 2000, "telnyx": 2001}, ["parasail", "telnyx", "venice"]),
    ],
)
def test_health_floor_never_promotes_degraded_routes(
    routes: list[ModelEndpoint], monkeypatch: pytest.MonkeyPatch,
    health: dict[str, int], expected: list[str],
) -> None:
    monkeypatch.setattr("trusted_router.routing.measured_provider_rank", lambda provider, sort: health[provider])
    assert _providers({}) == expected


@pytest.mark.parametrize(
    "body",
    [
        {"input_modalities": ["text", "image"]},
        {"requested_parameters": ["tools"], "provider": {"require_parameters": True}},
        {"provider": {"max_price": {"prompt": "0.15"}}},
    ],
)
def test_venice_remains_usable_when_only_it_meets_requirements(
    routes: list[ModelEndpoint], body: dict[str, Any],
) -> None:
    assert _providers(body) == ["venice"]


def test_confidential_requirement_still_excludes_venice(
    routes: list[ModelEndpoint], monkeypatch: pytest.MonkeyPatch,
) -> None:
    serve_on_fixture_route(monkeypatch, MODEL, "tinfoil", author="openai")
    assert _providers({"provider": {"min_privacy": "confidential"}}) == ["tinfoil"]


def test_venice_only_model_does_not_silently_change_model(
    routes: list[ModelEndpoint], monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = "unit/venice-only-uncensored"
    serve_on_fixture_route(monkeypatch, primary, "venice", author="openai")
    candidates = chat_route_endpoint_candidates(
        {"model": primary, "models": [MODEL]}, Settings(environment="test"),
    )
    assert [(model.id, endpoint.provider) for model, endpoint in candidates] == [
        (primary, "venice"), (MODEL, "parasail"), (MODEL, "telnyx"), (MODEL, "venice"),
    ]


@pytest.mark.parametrize("alias", ["trustedrouter/zdr", "nyte/zdr"])
@pytest.mark.parametrize(
    ("preferences", "expected"),
    [
        ({}, ["parasail", "venice"]),
        ({"order": ["venice"]}, ["venice", "parasail"]),
        ({"only": ["venice"], "allow_fallbacks": False}, ["venice"]),
        ({"order": ["venice"], "allow_fallbacks": False}, ["venice"]),
        ({"sort": "price"}, ["venice", "parasail"]),
    ],
)
def test_zdr_pool_uses_default_policy_but_preserves_user_selection(
    routes: list[ModelEndpoint], monkeypatch: pytest.MonkeyPatch,
    alias: str, preferences: dict[str, Any], expected: list[str],
) -> None:
    # This exact Venice model is policy-backed ZDR in the production metadata.
    model_id = "z-ai/glm-4.6"
    drop_routes(monkeypatch, model_id)
    for provider, price in (("venice", 100_000), ("parasail", 200_000)):
        serve_on_fixture_route(
            monkeypatch, model_id, provider, author="zai",
            prompt_price_microdollars_per_million_tokens=price,
        )
    monkeypatch.setattr(routing_candidates, "zdr_candidate_models", lambda: [MODELS[model_id]])
    assert _providers({"model": alias, "provider": preferences}) == expected


def test_zdr_model_pool_does_not_promote_venice_only_models(monkeypatch: pytest.MonkeyPatch) -> None:
    for endpoint_id in list(MODEL_ENDPOINTS):
        monkeypatch.delitem(MODEL_ENDPOINTS, endpoint_id)
    serve_on_fixture_route(monkeypatch, "z-ai/glm-4.6", "venice", author="zai")
    serve_on_fixture_route(
        monkeypatch, MODEL, "parasail", author="openai",
        prompt_price_microdollars_per_million_tokens=100_000,
    )
    assert [model.id for model in routing_candidates.zdr_candidate_models()] == [MODEL, "z-ai/glm-4.6"]


@pytest.mark.parametrize("route_type", ["chat.completions", "responses"])
def test_gateway_authorization_preserves_venice_as_fallback(
    routes: list[ModelEndpoint], route_type: str,
) -> None:
    client, key = _client_and_key()
    with client:
        response = client.post("/v1/internal/gateway/authorize", json={
            "api_key_hash": key["hash"], "model": MODEL,
            "estimated_input_tokens": 10, "max_output_tokens": 10,
            "route_type": route_type, "idempotency_key": f"venice-{route_type}",
        })
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["provider"] == "parasail"
    assert [row["provider"] for row in data["route_candidates"]] == ["parasail", "telnyx", "venice"]


def test_uncensored_glm_spelling_resolves_to_its_own_venice_endpoint(client, monkeypatch):
    existing = "z-ai/glm-5-3-flash-uncensored"
    dotted = "z-ai/glm-5.3-flash-uncensored"
    native = "glm-5-3-flash-uncensored"
    serve_on_fixture_route(monkeypatch, existing, "venice", author="zai", upstream_id=native)
    assert canonical_model_id(dotted) == existing
    assert canonical_model_id(dotted + ":nitro") == existing
    assert canonical_model_id("z-ai/glm-5.3-flash") != existing
    routes = client.get(f"/v1/models/{dotted}/endpoints")
    assert routes.status_code == 200
    assert routes.json() == client.get(f"/v1/models/{existing}/endpoints").json()
    assert {r["upstream_id"] for r in routes.json()["data"] if r["provider"] == "venice"} == {native}
