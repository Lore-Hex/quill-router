from __future__ import annotations

from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    PROVIDERS,
)


def test_discovery_does_not_activate_unfunded_provider_routes() -> None:
    endpoint_providers = {endpoint.provider for endpoint in MODEL_ENDPOINTS.values()}
    for provider_slug in (
        "baidu",
        "byteplus",
        "darkbloom",
        "huggingface",
        "poolside",
        "tencent",
    ):
        provider = PROVIDERS[provider_slug]
        assert provider.supports_prepaid is False
        assert provider.supports_byok is False
        assert provider_slug not in GATEWAY_PREPAID_PROVIDER_SLUGS
        assert provider_slug not in endpoint_providers
