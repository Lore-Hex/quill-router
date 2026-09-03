"""Mistral account-level training controls must not be represented as ZDR."""

from fastapi.testclient import TestClient

from trusted_router.catalog import provider_to_openrouter_shape
from trusted_router.catalog_data import PRIVACY_TIER_STANDARD, PROVIDERS
from trusted_router.catalog_privacy import provider_privacy_tier


def test_mistral_training_opt_out_remains_standard_privacy() -> None:
    provider = PROVIDERS["mistral"]
    public = provider_to_openrouter_shape(provider)

    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is False
    assert provider_privacy_tier(provider) == PRIVACY_TIER_STANDARD
    assert public["provider_zero_data_retention"] is False
    assert public["zero_data_retention_scope"] is None
    assert "model-training use disabled" in provider.provider_policy
    assert "TR-funded routes" in provider.provider_policy
    assert "not zero data retention" in provider.provider_policy.lower()
    assert "30 rolling days" in provider.provider_policy
    assert "Customer BYOK accounts" in provider.provider_policy
    assert provider.provider_policy_url == "https://legal.mistral.ai/terms/privacy-policy/"


def test_mistral_public_page_explains_training_opt_out_without_zdr(
    client: TestClient,
) -> None:
    response = client.get("/providers/mistral")

    assert response.status_code == 200
    assert "model-training use disabled" in response.text
    assert "TR-funded routes" in response.text
    assert "30 rolling days" in response.text
    assert "This is not zero data retention" in response.text

    zdr_providers = {
        row["provider"] for row in client.get("/v1/endpoints/zdr").json()["data"]
    }
    assert "mistral" not in zdr_providers
