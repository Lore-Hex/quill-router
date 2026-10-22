"""Trusted is an operator allowlist AND Confidential, never a soft preference."""

from dataclasses import replace
from itertools import permutations, product

import pytest
from bs4 import BeautifulSoup
from fastapi import HTTPException
from fastapi.testclient import TestClient

from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_TRUSTED,
    PROVIDERS,
    TRUSTED_MODEL_ID,
    TRUSTED_PROVIDER_SLUGS,
    Model,
    ModelEndpoint,
    endpoint_meets_privacy_requirement,
    endpoint_privacy_tier,
    model_provider_privacy_tier,
    model_to_openrouter_shape,
    provider_privacy_tier,
    provider_to_openrouter_shape,
)
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates, resolve_model_alias
from trusted_router.routing_snapshot import live_requested_models


@pytest.fixture
def pinned_model(monkeypatch: pytest.MonkeyPatch) -> str:
    model_id = "test/trusted-fixture"
    monkeypatch.setitem(MODELS, model_id, Model(
        id=model_id, name="Trusted fixture", provider="tinfoil",
        context_length=128_000, prepaid_available=True,
    ))
    for slug in ("tinfoil", "privatemode", "phala", "openai"):
        # Even an otherwise Confidential third operator cannot enter Trusted.
        monkeypatch.setitem(PROVIDERS, slug, replace(
            PROVIDERS[slug], provider_confidential_compute=True,
            provider_e2ee=True, provider_zero_data_retention=True,
        ))
        endpoint = ModelEndpoint(
            id=f"{model_id}@{slug}/prepaid", model_id=model_id, provider=slug,
            usage_type="Credits", upstream_id="phala/fixture" if slug == "phala" else "fixture",
            prompt_price_microdollars_per_million_tokens=1_000_000,
            completion_price_microdollars_per_million_tokens=2_000_000,
        )
        monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)
    return model_id


@pytest.mark.parametrize("alias", [TRUSTED_MODEL_ID, "nyte/trusted"])
@pytest.mark.parametrize("preferences", [{}, {"min_privacy": "any"}, {"order": ["openai", "phala"]}])
def test_alias_never_routes_outside_trusted(alias, preferences) -> None:
    candidates = chat_route_endpoint_candidates({"model": alias, "provider": preferences}, Settings())
    assert candidates
    assert {endpoint.provider for _, endpoint in candidates} <= TRUSTED_PROVIDER_SLUGS
    assert all(endpoint_meets_privacy_requirement(e, PRIVACY_TIER_TRUSTED) for _, e in candidates)


@pytest.mark.parametrize("models", list(permutations(["nyte/trusted", "trustedrouter/e2e", "test/trusted-fixture"])))
def test_fallback_order_cannot_dilute_trusted(pinned_model, models) -> None:
    candidates = chat_route_endpoint_candidates({"models": list(models)}, Settings())
    assert candidates
    assert {e.provider for _, e in candidates} <= TRUSTED_PROVIDER_SLUGS


def test_specific_model_can_require_trusted(pinned_model) -> None:
    candidates = chat_route_endpoint_candidates({
        "model": pinned_model, "provider": {"min_privacy": "trusted"},
    }, Settings())
    assert {e.provider for _, e in candidates} == {"tinfoil", "privatemode"}


@pytest.mark.parametrize("provider", [
    {"only": ["openai"]}, {"only": ["phala"]},
    {"order": ["openai"], "allow_fallbacks": False},
    {"ignore": ["tinfoil", "privatemode"]},
])
def test_conflicting_provider_filter_fails_closed(provider) -> None:
    with pytest.raises(HTTPException):
        chat_route_endpoint_candidates({"model": "nyte/trusted", "provider": provider}, Settings())


@pytest.mark.parametrize("slug", ["tinfoil", "privatemode"])
@pytest.mark.parametrize("compute,e2ee,zdr", list(product([True, False, None], repeat=3)))
def test_trusted_operator_still_requires_every_confidential_property(
    monkeypatch, slug, compute, e2ee, zdr,
) -> None:
    monkeypatch.setitem(PROVIDERS, slug, replace(
        PROVIDERS[slug], provider_confidential_compute=compute, provider_e2ee=e2ee,
        provider_zero_data_retention=zdr, prepaid_zero_data_retention=False,
    ))
    endpoint = ModelEndpoint(id="test", model_id="test/model", provider=slug, usage_type="Credits")
    expected = compute is True and e2ee is True and zdr is True
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_TRUSTED) is expected
    assert (endpoint_privacy_tier(endpoint) == PRIVACY_TIER_TRUSTED) is expected
    assert (provider_privacy_tier(PROVIDERS[slug]) == PRIVACY_TIER_TRUSTED) is expected


def test_empty_trusted_pool_never_falls_back_to_other_confidential_providers(monkeypatch) -> None:
    for slug in TRUSTED_PROVIDER_SLUGS:
        monkeypatch.setitem(PROVIDERS, slug, replace(
            PROVIDERS[slug], provider_e2ee=False,
        ))
    with pytest.raises(HTTPException):
        chat_route_endpoint_candidates({
            "model": "nyte/trusted", "models": ["trustedrouter/e2e", "trustedrouter/auto"],
        }, Settings())


def test_catalog_and_snapshot_publish_the_same_trusted_contract() -> None:
    assert resolve_model_alias("nyte/trusted") == TRUSTED_MODEL_ID
    payload = model_to_openrouter_shape(MODELS[TRUSTED_MODEL_ID])
    tr = payload["trustedrouter"]
    assert "nyte/trusted" in tr["aliases"]
    assert tr["route_kind"] == "trusted_pool"
    assert tr["required_provider_slugs"] == ["tinfoil", "privatemode"]
    assert tr["required_privacy_tier"] == PRIVACY_TIER_TRUSTED
    snapshot_model = next(m for m in live_requested_models(Settings()) if m.id == TRUSTED_MODEL_ID)
    assert snapshot_model.overrides.privacy_requirements == (PRIVACY_TIER_TRUSTED,)
    assert snapshot_model.expands_to
    for slug, provider in PROVIDERS.items():
        assert provider_to_openrouter_shape(provider)["trusted_provider"] is (slug in TRUSTED_PROVIDER_SLUGS)


def test_all_catalog_trusted_endpoints_are_also_confidential() -> None:
    for endpoint in MODEL_ENDPOINTS.values():
        expected = endpoint.provider in TRUSTED_PROVIDER_SLUGS and endpoint_meets_privacy_requirement(
            endpoint, PRIVACY_TIER_CONFIDENTIAL,
        )
        assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_TRUSTED) is expected


def test_a_declared_tier_cannot_replace_missing_technical_evidence(monkeypatch) -> None:
    from trusted_router.catalog_data import (
        _MODEL_PROVIDER_PRIVACY_OVERRIDES,
        ModelProviderPrivacyOverride,
    )

    monkeypatch.setitem(_MODEL_PROVIDER_PRIVACY_OVERRIDES, ("test/model", "tinfoil"),
        ModelProviderPrivacyOverride(privacy_tier=PRIVACY_TIER_TRUSTED,
            provider_zero_data_retention=False, provider_e2ee=False))
    assert model_provider_privacy_tier("test/model", "tinfoil") < PRIVACY_TIER_CONFIDENTIAL


def test_public_labels_and_docs(client: TestClient) -> None:
    soup = BeautifulSoup(client.get("/providers").text, "html.parser")
    trusted = {
        card["data-provider-id"] for card in soup.select("[data-provider-row]")
        if card.select('[data-privacy="trusted"]')
    }
    assert trusted == {"tinfoil", "privatemode"}
    assert soup.select_one('select[data-provider-privacy] option[value="trusted"]')
    assert {card["data-provider-id"] for card in soup.select('[data-trusted="true"]')} == trusted
    filtered = BeautifulSoup(client.get("/models?filter=trusted").text, "html.parser")
    assert filtered.select_one('a.good[href="/models?filter=trusted"]')
    assert filtered.select('[data-privacy="trusted"]')
    docs = client.get("/docs")
    assert docs.status_code == 200
    assert 'id="trusted-tier"' in docs.text
    assert "nyte/trusted" in docs.text
    assert "separate from hardware attestation" in docs.text
    assert client.get("/models/trustedrouter/trusted").status_code == 200


def test_trusted_disallows_unqualified_search_processors() -> None:
    from trusted_router.routes.internal.gateway import (
        _is_web_search_restricted_model,
        _is_web_search_restricted_provider,
    )

    assert _is_web_search_restricted_model("nyte/trusted")
    assert _is_web_search_restricted_model(TRUSTED_MODEL_ID)
    assert _is_web_search_restricted_model("nyte/trusted:online")
    assert _is_web_search_restricted_model("trustedrouter/auto", ["nyte/trusted"])
    assert _is_web_search_restricted_provider({"min_privacy": "trusted"})
