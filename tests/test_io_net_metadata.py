from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from trusted_router import routing
from trusted_router.catalog import (
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_STANDARD,
    PRIVACY_TIER_ZERO_RETENTION,
    PROVIDERS,
    provider_privacy_tier,
)
from trusted_router.catalog_data import PROVIDER_JURISDICTION_UNVERIFIED, Model, ModelEndpoint
from trusted_router.catalog_privacy import endpoint_meets_privacy_requirement
from trusted_router.config import Settings
from trusted_router.dashboard import public_provider_detail_html
from trusted_router.provider_branding import PROVIDER_BRANDS
from trusted_router.provider_locations import inference_location_metadata, provider_geography
from trusted_router.routing import chat_route_endpoint_candidates


@pytest.fixture
def io_net_endpoint() -> ModelEndpoint:
    # Privacy and jurisdiction metadata must not depend on a live model's lifetime.
    return ModelEndpoint(
        id="io-net/metadata-test@io-net:credits", model_id="io-net/metadata-test",
        provider="io-net", usage_type="Credits",
    )


def test_io_net_operator_is_us_but_inference_is_not_us_only(
    monkeypatch: pytest.MonkeyPatch, io_net_endpoint: ModelEndpoint,
) -> None:
    provider = PROVIDERS["io-net"]
    assert provider.provider_headquarters_country == "US"
    assert "io-net" not in PROVIDER_JURISDICTION_UNVERIFIED
    geography = provider_geography("io-net")
    assert geography["operator_country"] == "US"
    inference = geography["inference"]
    assert inference["locations"] == ("United States", "Canada")
    assert "Provider-submitted" in inference["evidence"]
    assert "no US-only" in inference["declaration"]
    assert "Not supported" in inference["trustedrouter_pinning"]
    assert "operating address" in geography["headquarters"]["location"]

    monkeypatch.setitem(routing.MODELS, io_net_endpoint.model_id, Model(
        id=io_net_endpoint.model_id, name="Metadata test", provider="io-net",
        context_length=4096, prepaid_available=True,
    ))
    monkeypatch.setattr(routing, "endpoints_for_model", lambda _model_id: [io_net_endpoint])
    candidates = chat_route_endpoint_candidates(
        {"model": io_net_endpoint.model_id, "provider": {"only": ["io-net"], "jurisdiction": "us"}},
        Settings(environment="test"),
    )
    assert candidates
    assert all(endpoint.provider == "io-net" for _model, endpoint in candidates)
    location = inference_location_metadata("io-net", io_net_endpoint.model_id)
    assert location["provider_declared_locations"] == ["United States", "Canada"]
    assert location["serving_region"] is None
    assert location["region_pinning_enforced"] is False


def test_io_net_declaration_does_not_promote_privacy_without_applicable_terms(
    io_net_endpoint: ModelEndpoint,
) -> None:
    provider = PROVIDERS["io-net"]
    assert provider_privacy_tier(provider) == PRIVACY_TIER_STANDARD
    assert provider.provider_zero_data_retention is not True
    assert provider.prepaid_zero_data_retention is False
    assert provider.provider_confidential_compute is not True
    assert provider.provider_e2ee is not True
    assert provider.provider_policy_url == "https://io.net/privacy"
    for claim in ("not stored or logged", "not used for training", "Content-free",
                  "provider declaration", "DPA has not been reviewed", "Standard pending"):
        assert claim in provider.provider_policy
    assert not endpoint_meets_privacy_requirement(io_net_endpoint, PRIVACY_TIER_ZERO_RETENTION)
    assert not endpoint_meets_privacy_requirement(io_net_endpoint, PRIVACY_TIER_CONFIDENTIAL)


def test_io_net_public_profile_excludes_private_contacts(test_settings: Settings) -> None:
    html = public_provider_detail_html(test_settings, "io-net")
    assert html is not None
    for text in ("io.net, Inc.", "Delaware", "West Hollywood", "99-2468828",
                 "Gaurav Sharma", "SOC 2", "audit report not reviewed", "Report pending",
                 "https://trust.io.net", "https://io.net/terms", "Canada"):
        assert text in html
    for label, value in (*PROVIDER_BRANDS["io-net"].company_details,
                         *PROVIDER_BRANDS["io-net"].resources):
        assert "@" not in value
        assert not re.search(r"\+\d[\d ()-]{6,}", value)
        assert not re.search(r"contact|phone|signer", label, re.I)
    for private in ("kate@io.net", "raj@io.net", "mark.roszak@io.net", "912 784 285"):
        assert private not in html


def test_io_net_provider_api_preserves_evidence_scope(client: TestClient) -> None:
    response = client.get("/v1/providers")
    assert response.status_code == 200
    provider = next(row for row in response.json()["data"] if row["id"] == "io-net")
    assert provider["provider_us_based"] is True
    assert provider["provider_zero_data_retention"] is not True
    assert provider["provider_e2ee"] is not True
    assert provider["geography"]["inference"]["locations"] == ["United States", "Canada"]
    assert "provider declaration" in provider["provider_policy"]
