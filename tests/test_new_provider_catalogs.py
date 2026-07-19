from __future__ import annotations

import json
from pathlib import Path

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.providers import chutes, cloudflare_workers_ai, digitalocean
from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    PROVIDERS,
)


def test_digitalocean_official_pricing_uses_integer_microdollars() -> None:
    markdown = """
| DeepSeek | [DeepSeek V4 Flash](https://example.test) | Input/output tokens | $0.112 per 1M tokens<br>$0.224 per 1M tokens<br>Prompt caching $0.028 per 1M tokens |
| Z.AI | [GLM-5.2](https://example.test) | Input/output tokens | $1.05 per 1M tokens<br>$4.40 per 1M tokens<br>Prompt caching $0.21 per 1M tokens |
"""

    prices = digitalocean._official_prices(markdown)

    assert prices["deepseek/deepseek-v4-flash"] == ModelPrice(
        prompt_micro_per_m=112_000,
        completion_micro_per_m=224_000,
        prompt_cached_micro_per_m=28_000,
    )
    assert prices["z-ai/glm-5.2"] == ModelPrice(
        prompt_micro_per_m=1_050_000,
        completion_micro_per_m=4_400_000,
        prompt_cached_micro_per_m=210_000,
    )


def test_cloudflare_uses_public_model_name_not_opaque_catalog_uuid() -> None:
    assert (
        cloudflare_workers_ai._canonical_model_id("@cf/moonshotai/kimi-k2.7-code")
        == "moonshotai/kimi-k2.7-code"
    )
    assert (
        cloudflare_workers_ai._canonical_model_id("@cf/meta/llama-4-scout")
        == "meta-llama/llama-4-scout"
    )
    assert (
        cloudflare_workers_ai._canonical_model_id("@cf/zai-org/glm-5.2")
        == "z-ai/glm-5.2"
    )


def test_cloudflare_pricing_parses_input_cache_and_output_units() -> None:
    price = cloudflare_workers_ai._model_price(
        {
            "price": [
                {"unit": "per M input tokens", "price": "0.95"},
                {"unit": "per M cached input tokens", "price": "0.19"},
                {"unit": "per M output tokens", "price": "4.00"},
            ]
        }
    )

    assert price == ModelPrice(
        prompt_micro_per_m=950_000,
        completion_micro_per_m=4_000_000,
        prompt_cached_micro_per_m=190_000,
    )


def test_cloudflare_committed_manifest_is_fail_closed_until_funded() -> None:
    manifest = json.loads(cloudflare_workers_ai.MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = manifest["models"]

    assert rows
    assert all(row.get("routable") is False for row in rows)
    assert all(
        row.get("routable_reason") in {"account-unfunded", "awaiting-price"}
        for row in rows
    )
    assert all(
        row["upstream_id"].startswith("@cf/")
        or row["upstream_id"] == "moonshotai/kimi-k3"
        for row in rows
    )
    kimi_k3 = next(row for row in rows if row["id"] == "moonshotai/kimi-k3")
    assert kimi_k3["upstream_id"] == "moonshotai/kimi-k3"
    assert kimi_k3["context_length"] == 1_048_576


def test_discovered_writer_never_routes_a_new_unpriced_model(tmp_path: Path) -> None:
    manifest_path = tmp_path / "provider.json"
    manifest_path.write_text(
        json.dumps({"provider": "example", "models": []}) + "\n",
        encoding="utf-8",
    )
    result = ProviderPricingResult(
        slug="example",
        prices={},
        source="api",
        fetched_url="https://example.test/models",
    )

    from scripts.pricing.manifest import write_discovered_chat_manifest

    write_discovered_chat_manifest(
        result,
        manifest_path=manifest_path,
        discovered_rows={
            "example/model": {
                "id": "example/model",
                "upstream_id": "native-model",
                "display_name": "Example Model",
            }
        },
        source_url="https://example.test/models",
    )

    row = json.loads(manifest_path.read_text(encoding="utf-8"))["models"][0]
    assert row["routable"] is False
    assert row["routable_reason"] == "awaiting-price"


def test_new_provider_privacy_and_gateway_registration() -> None:
    assert {"chutes", "digitalocean", "cloudflare-workers-ai"}.issubset(
        GATEWAY_PREPAID_PROVIDER_SLUGS
    )
    assert PROVIDERS["chutes"].provider_zero_data_retention is True
    assert PROVIDERS["chutes"].provider_confidential_compute is True
    assert PROVIDERS["chutes"].provider_e2ee is False
    assert PROVIDERS["cloudflare-workers-ai"].supports_byok is False


def test_new_provider_manifests_create_only_eligible_routes() -> None:
    endpoints = list(MODEL_ENDPOINTS.values())

    assert any(endpoint.provider == "chutes" for endpoint in endpoints)
    assert any(endpoint.provider == "digitalocean" for endpoint in endpoints)
    assert not any(endpoint.provider == "cloudflare-workers-ai" for endpoint in endpoints)


def test_chutes_manifest_contains_only_confidential_compute_rows() -> None:
    manifest = json.loads(chutes.MANIFEST_PATH.read_text(encoding="utf-8"))

    assert manifest["models"]
    assert all(row["confidential_compute"] is True for row in manifest["models"])
    assert any(row["id"] == "z-ai/glm-5.2" for row in manifest["models"])


def test_digitalocean_manifest_preserves_exact_upstream_ids() -> None:
    manifest = json.loads(digitalocean.MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = {row["id"]: row for row in manifest["models"]}

    assert rows["deepseek/deepseek-v4-flash"]["upstream_id"] == "deepseek-4-flash"
    assert rows["z-ai/glm-5.2"]["upstream_id"] == "glm-5.2"
