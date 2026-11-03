from dataclasses import replace

import pytest
from fastapi import HTTPException

from trusted_router.catalog import (
    MODEL_ENDPOINTS,
    MODELS,
    PROVIDERS,
    model_to_openrouter_shape,
    provider_to_openrouter_shape,
)
from trusted_router.catalog_privacy import (
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_TRUSTED,
    PRIVACY_TIER_ZERO_RETENTION,
    endpoint_meets_privacy_requirement,
    endpoint_stores_content,
    endpoint_zero_data_retention_scope,
    provider_privacy_tier,
)
from trusted_router.config import Settings
from trusted_router.provider_og import provider_og_facts
from trusted_router.routing import chat_route_endpoint_candidates


def test_grok_zdr_is_a_managed_account_setting_not_a_provider_default():
    provider = PROVIDERS["grok"]
    assert provider.prepaid_zero_data_retention is True
    assert provider.prepaid_zero_data_retention_effective_on == "2026-11-02"
    assert provider.provider_zero_data_retention is not True
    assert provider_privacy_tier(provider) == PRIVACY_TIER_STANDARD
    assert not provider.provider_confidential_compute
    assert not provider.provider_e2ee
    shape = provider_to_openrouter_shape(provider)
    assert shape["prepaid_zero_data_retention"] is True
    assert shape["provider_zero_data_retention"] is not True


def test_grok_credits_qualify_for_zdr_but_byok_and_confidential_do_not():
    endpoint = MODEL_ENDPOINTS["x-ai/grok-4.7@grok/prepaid"]
    assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION)
    assert not endpoint_stores_content(endpoint)
    assert endpoint_zero_data_retention_scope(endpoint) == "trustedrouter_prepaid"
    for floor in (PRIVACY_TIER_CONFIDENTIAL, PRIVACY_TIER_TRUSTED):
        assert not endpoint_meets_privacy_requirement(endpoint, floor)
    byok = replace(endpoint, usage_type="BYOK")
    assert not endpoint_meets_privacy_requirement(byok, PRIVACY_TIER_ZERO_RETENTION)
    assert endpoint_stores_content(byok)
    assert endpoint_zero_data_retention_scope(byok) is None


def test_grok_public_model_metadata_keeps_credential_scopes_separate():
    shape = model_to_openrouter_shape(MODELS["x-ai/grok-4.7"])
    routes = [row for row in shape["trustedrouter"]["endpoints"] if row["provider"] == "grok"]
    assert {row["usage_type"] for row in routes} == {"Credits", "BYOK"}
    for row in routes:
        managed = row["usage_type"] == "Credits"
        assert (row["provider_zero_data_retention"] is True) == managed
        assert row["zero_data_retention_scope"] == ("trustedrouter_prepaid" if managed else None)
        assert not row["provider_confidential_compute"]
        assert not row["provider_e2ee"]


@pytest.mark.parametrize("floor", ["zdr", "confidential", "trusted"])
def test_grok_privacy_floor_routes_only_to_qualified_credentials(floor):
    request = {"model": "x-ai/grok-4.7", "provider": {
        "only": ["grok"], "allow_fallbacks": False, "min_privacy": floor,
    }}
    if floor == "zdr":
        candidates = chat_route_endpoint_candidates(request, Settings(environment="test"))
        assert candidates
        assert all(route.provider == "grok" and route.usage_type == "Credits" for _, route in candidates)
    else:
        with pytest.raises(HTTPException) as caught:
            chat_route_endpoint_candidates(request, Settings(environment="test"))
        assert caught.value.status_code == 400
        assert caught.value.detail["error"]["message"] == "No route candidates match the requested provider filters"
        assert caught.value.detail["error"]["source"] == "router"


def test_grok_provider_page_describes_the_managed_scope(client):
    response = client.get("/providers/grok")
    assert response.status_code == 200
    assert "TR-funded routes" in response.text
    assert "x-zero-data-retention" in response.text
    assert "BYOK" in response.text
    assert "not confidential compute" in response.text


@pytest.mark.parametrize("provider", ["grok", "openai", "google-vertex", "coralbricks"])
def test_managed_zdr_social_cards_do_not_claim_byok_coverage(provider):
    assert provider_og_facts(provider).privacy == "TR-funded ZDR"


def test_provider_wide_zdr_and_confidential_social_labels_are_unchanged():
    assert provider_og_facts("together").privacy == "Zero retention"
    assert provider_og_facts("maple").privacy == "Confidential + ZDR"
