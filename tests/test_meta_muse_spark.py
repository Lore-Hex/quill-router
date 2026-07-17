from __future__ import annotations

from scripts.ingest_openrouter_catalog import PROVIDER_NAME_TO_SLUG
from scripts.pricing import refresh
from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    MODELS,
    PROVIDERS,
)
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS

MODEL_ID = "meta/muse-spark-1.1"
ENDPOINT_ID = f"{MODEL_ID}@meta/prepaid"


def test_muse_spark_is_a_prepaid_openrouter_backed_route() -> None:
    provider = PROVIDERS["meta"]
    assert provider.name == "Meta via OpenRouter"
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False
    assert "OpenRouter" in provider.provider_policy
    assert provider.provider_policy_url

    assert "meta" in GATEWAY_PREPAID_PROVIDER_SLUGS
    model = MODELS[MODEL_ID]
    assert model.context_length == 1_048_576
    assert model.prepaid_available is False
    assert model.byok_available is False

    endpoint = MODEL_ENDPOINTS[ENDPOINT_ID]
    assert endpoint.provider == "meta"
    assert endpoint.usage_type == "Credits"
    assert endpoint.upstream_id == MODEL_ID
    assert endpoint.prompt_price_microdollars_per_million_tokens == 1_375_000
    assert endpoint.completion_price_microdollars_per_million_tokens == 4_675_000
    assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 165_000
    assert f"{MODEL_ID}@meta/byok" not in MODEL_ENDPOINTS


def test_meta_openrouter_route_stays_in_automated_catalog_refresh() -> None:
    assert PROVIDER_NAME_TO_SLUG["Meta"] == "meta"
    assert "meta" in refresh.PROVIDER_SLUGS
    assert OPENAI_COMPATIBLE_PROVIDERS["meta"] == (
        ("OPENROUTER_API_KEY",),
        "https://openrouter.ai/api/v1",
    )
