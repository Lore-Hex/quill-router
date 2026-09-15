from __future__ import annotations

import itertools

import pytest
from bs4 import BeautifulSoup
from fastapi import HTTPException

from trusted_router.catalog import MODELS, endpoints_for_model, meta_candidate_models
from trusted_router.config import Settings
from trusted_router.routing import _apply_endpoint_provider_filters, _routing_for_body


@pytest.fixture(autouse=True)
def current_catalog(monkeypatch):
    from trusted_router.catalog_data import ModelEndpoint

    # These are routing contracts, independent of the checked-in snapshot date.
    monkeypatch.setattr(ModelEndpoint, "catalog_is_current", lambda self, **_: True)


def test_green_alias_has_live_catalog_candidates() -> None:
    assert "trustedrouter/green" in MODELS
    assert meta_candidate_models("trustedrouter/green")


@pytest.mark.parametrize("other", ["trustedrouter/auto", "trustedrouter/zdr", "trustedrouter/eu"])
def test_green_requirement_survives_model_order_and_provider_preferences(other: str) -> None:
    for model, fallback in itertools.permutations(["trustedrouter/green", other]):
        ids, prefs = _routing_for_body(
            {"model": model, "models": [fallback], "provider": {"order": ["openai"]}},
            Settings(),
        )
        candidates = [
            (MODELS[mid], endpoint)
            for mid in ids if mid in MODELS
            for endpoint in endpoints_for_model(mid)
        ]
        selected = _apply_endpoint_provider_filters(candidates, prefs)
        assert selected
        assert {ep.provider for _, ep in selected} == {"regolo"}


def test_green_never_falls_back_when_explicit_provider_conflicts() -> None:
    try:
        ids, prefs = _routing_for_body(
            {"model": "trustedrouter/green", "provider": {"only": ["openai"]}},
            Settings(),
        )
    except HTTPException:
        return
    candidates = [
        (MODELS[mid], ep) for mid in ids if mid in MODELS
        for ep in endpoints_for_model(mid)
    ]
    assert not _apply_endpoint_provider_filters(candidates, prefs)


def test_green_is_not_an_e2ee_claim() -> None:
    from trusted_router.catalog_data import PROVIDERS

    assert PROVIDERS["regolo"].provider_zero_data_retention is True
    assert PROVIDERS["regolo"].provider_confidential_compute is not True
    assert PROVIDERS["regolo"].provider_e2ee is not True


@pytest.mark.parametrize("provider", [
    {"ignore": ["regolo"]}, {"min_privacy": "confidential"},
    {"order": ["openai"], "allow_fallbacks": False},
])
def test_green_conflicting_filters_fail_closed(provider):
    from trusted_router.routing import chat_route_endpoint_candidates

    with pytest.raises(HTTPException):
        chat_route_endpoint_candidates({"model": "trustedrouter/green", "provider": provider}, Settings())


def test_no_qualifying_provider_never_expands_to_other_routes(monkeypatch):
    from dataclasses import replace

    from trusted_router.catalog_data import PROVIDERS
    from trusted_router.routing import chat_route_endpoint_candidates

    monkeypatch.setitem(PROVIDERS, "regolo", replace(PROVIDERS["regolo"], renewable_energy_inference=False))
    with pytest.raises(HTTPException):
        chat_route_endpoint_candidates({"model": "trustedrouter/green"}, Settings())


def test_green_prices_and_privacy_describe_the_eligible_provider_only():
    from trusted_router.catalog import model_to_openrouter_shape

    shape = model_to_openrouter_shape(MODELS["trustedrouter/green"])
    eligible = [
        ep for m in meta_candidate_models("trustedrouter/green")
        for ep in endpoints_for_model(m.id) if ep.provider == "regolo" and ep.usage_type == "Credits"
    ]
    assert shape["trustedrouter"]["prompt_price_microdollars_per_million_tokens"] == min(
        ep.prompt_price_microdollars_per_million_tokens for ep in eligible
    )
    assert shape["trustedrouter"]["eligible_providers"] == ["regolo"]
    assert shape["trustedrouter"]["energy_claim_scope"] == "provider_inference"
    assert shape["trustedrouter"]["provider_e2ee"] is False


def test_green_page_has_sources_code_and_a_working_share_image(client):
    response = client.get("/green-tokens")
    assert response.status_code == 200
    assert 'model="trustedrouter/green"' in response.text
    assert 'data-action="copy-code"' in response.text
    assert "https://regolo.ai/sustainable-ai/" in response.text
    assert "provider-declared" in response.text.lower()
    assert 'href="https://trustedrouter.com/green-tokens"' in response.text
    assert client.get("/static/green-tokens-hero.webp").status_code == 200
    assert client.get("/static/og/green-tokens.png").status_code == 200
    assert "/green-tokens" in client.get("/").text


def test_green_landing_copy_is_provider_neutral_with_energy_sources(client):
    response = client.get("/green-tokens")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert "regolo" not in page.get_text(" ", strip=True).lower()
    for meta in page.select("meta[content]"):
        assert "regolo" not in str(meta["content"]).lower()
    for script in page.select('script[type="application/ld+json"]'):
        assert "regolo" not in script.get_text().lower()
    assert page.select_one('a[href="https://regolo.ai/sustainable-ai/"]')
    assert page.select_one('a[href="https://regolo.ai/zero-data-retention/"]')
    assert "provider-declared" in page.get_text().lower()
    assert "inference electricity" in page.get_text().lower()
    assert len(page.select(".green-models a")) >= 11


def test_expired_regolo_catalog_fails_closed(monkeypatch):
    from trusted_router.catalog_data import ModelEndpoint
    from trusted_router.routing import chat_route_endpoint_candidates

    monkeypatch.setattr(ModelEndpoint, "catalog_is_current", lambda self, **_: self.provider != "regolo")
    assert meta_candidate_models("trustedrouter/green") == []
    with pytest.raises(HTTPException):
        chat_route_endpoint_candidates({"model": "trustedrouter/green"}, Settings())


def test_green_hosted_search_cannot_leave_the_pool():
    from trusted_router.routes.internal.gateway import _is_web_search_restricted_model

    assert _is_web_search_restricted_model("trustedrouter/green")


@pytest.mark.parametrize("route_type", ["chat.completions", "responses"])
def test_green_authorizes_only_regolo_and_settles_selected_fallback_once(route_type):
    from tests.test_gateway_fallback_billing import _client_and_key
    from trusted_router.catalog import endpoint_for_id
    from trusted_router.routes.internal.gateway import _endpoint_cost_microdollars
    from trusted_router.storage import STORE

    client, key = _client_and_key()
    money = STORE.credit_money[key["workspace_id"]]
    usage_before = money.total_usage_microdollars
    authorized = client.post("/v1/internal/gateway/authorize", json={
        "api_key_hash": key["hash"], "model": "trustedrouter/green",
        "estimated_input_tokens": 1000, "max_output_tokens": 512,
        "route_type": route_type, "idempotency_key": f"green-{route_type}",
    })
    assert authorized.status_code == 200, authorized.text
    authorization = authorized.json()["data"]
    routes = authorization["route_candidates"]
    assert len(routes) > 1
    assert {route["provider"] for route in routes} == {"regolo"}
    assert {route["usage_type"] for route in routes} == {"Credits"}
    selected = routes[-1]
    endpoint = endpoint_for_id(selected["endpoint_id"])
    assert endpoint is not None
    expected = _endpoint_cost_microdollars(endpoint, 1000, 100)
    assert expected > 0
    body = {
        "authorization_id": authorization["authorization_id"],
        "selected_model": selected["model"], "selected_endpoint": endpoint.id,
        "actual_input_tokens": 1000, "actual_output_tokens": 100,
        "request_id": f"green-{route_type}", "route_type": route_type,
    }
    settled = client.post("/v1/internal/gateway/settle", json=body)
    assert settled.status_code == 200, settled.text
    assert settled.json()["data"]["cost_microdollars"] == expected
    replay = client.post("/v1/internal/gateway/settle", json=body)
    assert replay.status_code == 200, replay.text
    assert replay.json()["data"]["already_settled"] is True
    assert money.total_usage_microdollars == usage_before + expected
    assert money.reserved_microdollars == 0
