from __future__ import annotations

from dataclasses import replace

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS
from trusted_router.catalog_data import (
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_NO_STORE,
    PRIVACY_TIER_ZERO_RETENTION,
    ModelEndpoint,
)
from trusted_router.catalog_privacy import (
    endpoint_meets_privacy_requirement,
    endpoint_stores_content,
    endpoint_zero_data_retention,
)
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates

POLICY_URL = (
    "https://docs.privatemode.ai/security/trust-and-compliance/#data-processing-and-retention"
)


@pytest.fixture
def privatemode_route(monkeypatch: pytest.MonkeyPatch) -> ModelEndpoint:
    # Privacy is independent of live catalog availability and price refreshes.
    endpoint = ModelEndpoint(
        id="openai/gpt-oss-120b@privatemode/prepaid",
        model_id="openai/gpt-oss-120b",
        provider="privatemode",
        usage_type="Credits",
        upstream_id="gpt-oss-120b",
    )
    monkeypatch.setitem(MODEL_ENDPOINTS, endpoint.id, endpoint)
    return endpoint


def test_privatemode_zdr_is_explicit_and_scoped_to_content(privatemode_route: ModelEndpoint) -> None:
    provider = PROVIDERS["privatemode"]
    assert provider.provider_zero_data_retention is True
    assert provider.stores_content is False
    assert provider.supports_byok is False
    assert provider.provider_policy_url == POLICY_URL
    assert "not used for training" in provider.provider_policy
    assert "in-memory prompt cache" in provider.provider_policy
    assert "up to 90 days" in provider.provider_policy
    assert "permanently for billing" in provider.provider_policy
    assert "not metadata" in provider.provider_policy
    assert endpoint_zero_data_retention(privatemode_route) is True
    assert endpoint_stores_content(privatemode_route) is False
    for requirement in (
        PRIVACY_TIER_CONFIDENTIAL, PRIVACY_TIER_ZERO_RETENTION, PRIVACY_TIER_NO_STORE,
    ):
        assert endpoint_meets_privacy_requirement(privatemode_route, requirement)


@pytest.mark.parametrize("privacy", ["zdr", "confidential", "no_store"])
def test_privatemode_can_satisfy_pinned_privacy_routing(
    privatemode_route: ModelEndpoint, privacy: str,
) -> None:
    candidates = chat_route_endpoint_candidates(
        {
            "model": privatemode_route.model_id,
            "provider": {"only": ["privatemode"], "min_privacy": privacy},
        },
        Settings(environment="test"),
    )
    assert candidates
    assert all(endpoint.provider == "privatemode" for _model, endpoint in candidates)
    assert all(endpoint_zero_data_retention(endpoint) is True for _model, endpoint in candidates)


@pytest.mark.parametrize("zdr", [None, False])
def test_confidential_compute_alone_does_not_grant_zdr(
    monkeypatch: pytest.MonkeyPatch, privatemode_route: ModelEndpoint, zdr: bool | None,
    client: TestClient,
) -> None:
    monkeypatch.setitem(PROVIDERS, "privatemode", replace(
        PROVIDERS["privatemode"], provider_zero_data_retention=zdr, stores_content=True,
    ))
    assert endpoint_meets_privacy_requirement(privatemode_route, PRIVACY_TIER_CONFIDENTIAL)
    assert not endpoint_meets_privacy_requirement(privatemode_route, PRIVACY_TIER_ZERO_RETENTION)
    assert not endpoint_meets_privacy_requirement(privatemode_route, PRIVACY_TIER_NO_STORE)
    assert endpoint_zero_data_retention(privatemode_route) is zdr
    listed = client.get("/v1/endpoints/zdr").json()["data"]
    assert "privatemode" not in {item["provider"] for item in listed}


def test_privatemode_public_metadata_and_badges_show_both_claims(
    client: TestClient, privatemode_route: ModelEndpoint,
) -> None:
    providers = {item["id"]: item for item in client.get("/v1/providers").json()["data"]}
    provider = providers["privatemode"]
    assert provider["provider_zero_data_retention"] is True
    assert provider["stores_content"] is False
    assert provider["zero_data_retention_scope"] == "provider"
    assert provider["provider_confidential_compute"] is True
    assert provider["provider_e2ee"] is True
    assert provider["provider_policy_url"] == POLICY_URL

    zdr = client.get("/v1/endpoints/zdr").json()["data"]
    assert "privatemode" in {item["provider"] for item in zdr}
    assert all(
        item["provider_zero_data_retention"] is True or item["prepaid_zero_data_retention"]
        for item in zdr
    )
    endpoints = client.get(f"/v1/models/{privatemode_route.model_id}/endpoints").json()["data"]
    endpoint = next(item for item in endpoints if item["endpoint_id"] == privatemode_route.id)
    assert endpoint["trustedrouter"]["provider_zero_data_retention"] is True
    assert endpoint["trustedrouter"]["provider_e2ee"] is True
    assert endpoint["trustedrouter"]["provider_policy_url"] == POLICY_URL
    soup = BeautifulSoup(client.get("/providers").text, "html.parser")
    card = soup.select_one('[data-provider-id="privatemode"]')
    assert card is not None
    assert card.select_one('[data-privacy="confidential"]') is not None
    assert card.select_one('[data-privacy="zdr"]') is not None

    detail = client.get("/providers/privatemode")
    assert detail.status_code == 200
    assert POLICY_URL in detail.text
    assert "up to 90 days" in detail.text
    assert "in-memory prompt cache" in detail.text
