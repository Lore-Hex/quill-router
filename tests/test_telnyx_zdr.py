from __future__ import annotations

from fastapi.testclient import TestClient

from trusted_router.catalog import (
    PRIVACY_TIER_CONFIDENTIAL,
    PRIVACY_TIER_ZERO_RETENTION,
    PROVIDERS,
    endpoints_for_model,
    provider_privacy_tier,
)
from trusted_router.catalog_privacy import (
    endpoint_confidential_compute,
    endpoint_e2ee,
    endpoint_meets_privacy_requirement,
    endpoint_stores_content,
    endpoint_zero_data_retention,
)
from trusted_router.config import Settings
from trusted_router.routing import chat_route_endpoint_candidates

POLICY_URL = "https://telnyx.com/privacy-policy"


def test_telnyx_hosted_chat_routes_are_zdr_not_confidential() -> None:
    provider = PROVIDERS["telnyx"]
    assert provider.stores_content is False
    assert provider.provider_zero_data_retention is True
    assert provider_privacy_tier(provider) == PRIVACY_TIER_ZERO_RETENTION
    assert provider.provider_policy_url == POLICY_URL
    assert "/v2/ai/openai/chat/completions" in provider.provider_policy
    assert "adapts Responses to chat completions" in provider.provider_policy
    assert "Stateful Responses" in provider.provider_policy
    assert "outside this ZDR scope" in provider.provider_policy

    endpoints = [
        endpoint for endpoint in endpoints_for_model("z-ai/glm-5.2")
        if endpoint.provider == "telnyx"
    ]
    assert {endpoint.usage_type for endpoint in endpoints} == {"Credits", "BYOK"}
    for endpoint in endpoints:
        assert endpoint_zero_data_retention(endpoint) is True
        assert endpoint_stores_content(endpoint) is False
        assert endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_ZERO_RETENTION)
        assert not endpoint_meets_privacy_requirement(endpoint, PRIVACY_TIER_CONFIDENTIAL)
        assert endpoint_confidential_compute(endpoint) is not True
        assert endpoint_e2ee(endpoint) is not True


def test_telnyx_can_satisfy_pinned_zdr_routing() -> None:
    candidates = chat_route_endpoint_candidates(
        {
            "model": "z-ai/glm-5.2",
            "provider": {"only": ["telnyx"], "min_privacy": "zdr"},
        },
        Settings(environment="test"),
    )
    assert candidates
    assert all(endpoint.provider == "telnyx" for _model, endpoint in candidates)
    assert all(endpoint_zero_data_retention(endpoint) is True for _model, endpoint in candidates)


def test_telnyx_public_zdr_metadata_has_evidence_and_endpoint_scope(client: TestClient) -> None:
    providers = {item["id"]: item for item in client.get("/v1/providers").json()["data"]}
    telnyx = providers["telnyx"]
    assert telnyx["provider_zero_data_retention"] is True
    assert telnyx["stores_content"] is False
    assert telnyx["zero_data_retention_scope"] == "provider"
    assert telnyx["provider_policy_url"] == POLICY_URL
    assert telnyx["provider_e2ee"] is not True
    assert telnyx["provider_confidential_compute"] is not True

    zdr = client.get("/v1/endpoints/zdr").json()["data"]
    assert "telnyx" in {item["provider"] for item in zdr}
    html = client.get("/providers/telnyx").text
    assert POLICY_URL in html
    assert "/v2/ai/openai/chat/completions" in html
    assert "Stateful Responses" in html
    assert "outside this ZDR scope" in html
    assert "de-identified under 3.5" in html
