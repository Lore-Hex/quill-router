from __future__ import annotations

from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
from scripts.ingest_openrouter_catalog import PROVIDER_NAME_TO_SLUG
from scripts.pricing import refresh
from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    MODELS,
    PROVIDERS,
)
from trusted_router.catalog_ingest import _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS
from trusted_router.providers import OPENAI_COMPATIBLE_PROVIDERS

MODEL_ID = "meta/muse-spark-1.1"
ENDPOINT_ID = f"{MODEL_ID}@meta/prepaid"


def test_muse_spark_routes_use_direct_meta_without_claiming_zdr() -> None:
    provider = PROVIDERS["meta"]
    assert provider.name == "Meta"
    assert provider.supports_prepaid is True
    assert provider.supports_byok is False
    assert provider.stores_content is True
    assert provider.provider_zero_data_retention is False
    assert provider.provider_confidential_compute is False
    assert provider.provider_e2ee is False
    assert "directly" in provider.provider_policy
    assert "OpenRouter" not in provider.provider_policy
    assert provider.provider_policy_url

    assert "meta" in GATEWAY_PREPAID_PROVIDER_SLUGS
    assert {endpoint.model_id for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "meta"} == {
        f"meta/muse-spark-{version}" for version in ("1.1", "1.2", "1.3")
    }
    for version in ("1.1", "1.2", "1.3"):
        model_id = f"meta/muse-spark-{version}"
        assert model_id in MODELS
        assert MODEL_ENDPOINTS[f"{model_id}@meta/prepaid"].upstream_id == f"muse-spark-{version}"
    assert not any("contributor" in key for key in MODELS if key.startswith("meta/"))
    assert f"{MODEL_ID}@meta/byok" not in MODEL_ENDPOINTS


def test_direct_meta_stays_in_automated_catalog_refresh() -> None:
    assert PROVIDER_NAME_TO_SLUG["Meta"] == "meta"
    assert "meta" in refresh.PROVIDER_SLUGS
    assert "meta" in _AUTHORITATIVE_PROVIDER_MANIFEST_SLUGS
    entries = [row for row in _DISCOVERABLE_MANIFEST_PROVIDERS if row[0] == "meta"]
    assert len(entries) == 1
    _, url, envs, normalize = entries[0]
    assert url == "https://api.meta.ai/v1/models"
    assert envs == ("META_API_KEY",)
    assert normalize("muse-spark-1.3") == "meta/muse-spark-1.3"
    assert normalize("muse-spark-1.3-contributor") is None
    assert normalize("muse-image-1.0") is None
    assert OPENAI_COMPATIBLE_PROVIDERS["meta"] == (
        ("META_API_KEY",),
        "https://api.meta.ai/v1",
    )
