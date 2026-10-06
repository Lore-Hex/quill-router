from __future__ import annotations

from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    PROVIDERS,
)


def test_discovery_does_not_activate_unready_provider_routes() -> None:
    endpoint_providers = {endpoint.provider for endpoint in MODEL_ENDPOINTS.values()}
    for provider_slug in (
        "baidu",
        "darkbloom",
        "huggingface",
        "poolside",
    ):
        provider = PROVIDERS[provider_slug]
        assert provider.supports_prepaid is False
        assert provider.supports_byok is False
        assert provider_slug not in GATEWAY_PREPAID_PROVIDER_SLUGS
        assert provider_slug not in endpoint_providers


def test_byteplus_activation_is_not_confused_with_router_readiness() -> None:
    provider = PROVIDERS["byteplus"]
    assert "Direct BytePlus" in provider.provider_policy
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert "byteplus" in GATEWAY_PREPAID_PROVIDER_SLUGS
