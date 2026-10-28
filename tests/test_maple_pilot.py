import pytest

from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    MODELS,
    PROVIDERS,
    provider_is_routable,
    provider_to_openrouter_shape,
    providers_for_display,
)
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS


def test_maple_pilot_cannot_receive_customer_traffic_or_claim_privacy() -> None:
    provider = PROVIDERS["maple"]
    assert provider.internal_only
    assert not provider.supports_prepaid
    assert not provider.supports_byok
    assert not provider.attested_gateway
    assert not provider.provider_zero_data_retention
    assert not provider.provider_confidential_compute
    assert not provider.provider_e2ee
    assert not provider_is_routable(provider)
    assert provider_to_openrouter_shape(provider)["routing_status"] == "blocked"
    assert provider not in providers_for_display()
    assert "maple" not in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert "maple" not in OPENAI_COMPATIBLE_PROVIDERS
    assert not any(endpoint.provider == "maple" for endpoint in MODEL_ENDPOINTS.values())
    assert not any(model.provider == "maple" for model in MODELS.values())


@pytest.mark.parametrize("path", ["/v1/providers", "/providers", "/models", "/sitemap.xml"])
def test_maple_pilot_is_not_a_public_listing(client, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert "maple" not in response.text.lower()


@pytest.mark.parametrize("path", ["/providers/maple", "/providers/maple/performance"])
def test_maple_pilot_cannot_be_published_by_guessing_its_slug(client, path: str) -> None:
    assert client.get(path).status_code == 404
