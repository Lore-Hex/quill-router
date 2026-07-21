from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.manifest import set_manifest_canary_state
from scripts.pricing.openai_catalog import discover_openai_chat_catalog
from scripts.pricing.parsers import morph as morph_parser
from scripts.pricing.parsers import streamlake as streamlake_parser
from scripts.pricing.providers import atlas_cloud, inceptron, morph, streamlake
from scripts.pricing.refresh import _PRICING_RESULT_PROVIDER_ALIASES, PROVIDER_SLUGS
from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    PROVIDERS,
)

ROOT = Path(__file__).resolve().parents[1]


def test_provider_owned_pricing_parsers_use_integer_microdollars() -> None:
    morph_prices = morph_parser.parse(
        (ROOT / "tests/fixtures/pricing/morph.html").read_text(encoding="utf-8")
    )
    streamlake_prices = streamlake_parser.parse(
        (ROOT / "tests/fixtures/pricing/streamlake.html").read_text(
            encoding="utf-8"
        )
    )

    assert morph_prices["z-ai/glm-5.2"] == {
        "prompt_micro_per_m": 1_100_000,
        "completion_micro_per_m": 4_100_000,
    }
    assert morph_prices["qwen/qwen3.5-397b-a17b"] == {
        "prompt_micro_per_m": 500_000,
        "prompt_cached_micro_per_m": 300_000,
        "completion_micro_per_m": 3_500_000,
    }
    assert streamlake_prices["kwaipilot/kat-coder-pro-v2.5"] == {
        "prompt_micro_per_m": 740_000,
        "completion_micro_per_m": 2_960_000,
        "prompt_cached_micro_per_m": 150_000,
    }


def test_morph_checkpoint_fallback_covers_published_live_chat_catalog() -> None:
    prices = morph_parser.parse("Vercel Security Checkpoint")

    assert set(prices) == set(morph_parser.MODEL_IDS.values())
    assert prices["morph/morph-v3-fast"] == {
        "prompt_micro_per_m": 800_000,
        "completion_micro_per_m": 1_200_000,
    }
    assert prices["qwen/qwen3.5-397b-a17b"]["prompt_cached_micro_per_m"] == 300_000


def test_openai_catalog_preserves_exact_ids_and_filters_non_text_output() -> None:
    upstream_ids: dict[str, str] = {}
    prices, rows = discover_openai_chat_catalog(
        [
            {
                "id": "Vendor/Exact-Model",
                "name": "Exact model",
                "context_length": 262_144,
                "max_output_tokens": 8_192,
                "output_modalities": ["text"],
                "pricing": {
                    "prompt": "0.0000005",
                    "completion": "0.0000035",
                    "input_cache_reads": "0.0000003",
                },
            },
            {
                "id": "Vendor/Image-Only",
                "output_modalities": ["image"],
                "pricing": {"prompt": "0.000001", "completion": "0.000001"},
            },
        ],
        explicit_map={"Vendor/Exact-Model": "vendor/exact-model"},
        upstream_id_map=upstream_ids,
    )

    assert prices == {
        "vendor/exact-model": ModelPrice(
            prompt_micro_per_m=500_000,
            completion_micro_per_m=3_500_000,
            prompt_cached_micro_per_m=300_000,
        )
    }
    assert rows["vendor/exact-model"]["upstream_id"] == "Vendor/Exact-Model"
    assert rows["vendor/exact-model"]["context_length"] == 262_144
    assert upstream_ids == {"vendor/exact-model": "Vendor/Exact-Model"}


def test_wave2_provider_privacy_and_gateway_registration() -> None:
    slugs = {"inceptron", "morph", "atlas-cloud", "streamlake"}
    assert slugs.issubset(GATEWAY_PREPAID_PROVIDER_SLUGS)
    assert PROVIDERS["inceptron"].provider_zero_data_retention is True
    assert PROVIDERS["inceptron"].stores_content is False
    for slug in slugs:
        assert PROVIDERS[slug].supports_prepaid is True
        assert PROVIDERS[slug].supports_byok is False
    for slug in {"morph", "atlas-cloud", "streamlake"}:
        assert PROVIDERS[slug].provider_zero_data_retention is not True


def test_wave2_manifests_publish_only_live_eligible_routes() -> None:
    manifests = {
        provider.SLUG: json.loads(provider.MANIFEST_PATH.read_text(encoding="utf-8"))
        for provider in (inceptron, morph, atlas_cloud, streamlake)
    }
    assert len(manifests["inceptron"]["models"]) == 4
    assert len(manifests["morph"]["models"]) == 8
    assert len(manifests["atlas-cloud"]["models"]) >= 100
    atlas_image_rows = [
        row
        for row in manifests["atlas-cloud"]["models"]
        if "image" in row["upstream_id"].casefold()
    ]
    assert atlas_image_rows
    assert all(
        row.get("routable") is False
        and row.get("routable_reason") == "delisted-upstream"
        for row in atlas_image_rows
    )
    assert all(row["upstream_id"] for raw in manifests.values() for row in raw["models"])
    assert all(
        row.get("routable") is False
        and row.get("routable_reason") == "provider-canary-failed"
        for row in manifests["streamlake"]["models"]
    )

    route_providers = {endpoint.provider for endpoint in MODEL_ENDPOINTS.values()}
    assert {"inceptron", "morph", "atlas-cloud"}.issubset(route_providers)
    assert "streamlake" not in route_providers


def test_wave2_exact_upstream_ids_are_committed() -> None:
    inceptron_rows = {
        row["id"]: row
        for row in json.loads(inceptron.MANIFEST_PATH.read_text())["models"]
    }
    atlas_rows = {
        row["id"]: row
        for row in json.loads(atlas_cloud.MANIFEST_PATH.read_text())["models"]
    }
    assert (
        inceptron_rows["moonshotai/kimi-k2.7-code"]["upstream_id"]
        == "moonshotai/Kimi-K2.7-Code"
    )
    assert atlas_rows["z-ai/glm-5.2"]["upstream_id"] == "zai-org/glm-5.2"


def test_streamlake_canary_state_is_machine_owned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "streamlake.json"
    manifest_path.write_text(
        json.dumps({"provider": "streamlake", "models": []}) + "\n",
        encoding="utf-8",
    )
    model_id = "kwaipilot/kat-coder-pro-v2"
    monkeypatch.setattr(streamlake, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        streamlake,
        "_DISCOVERED_MANIFEST_ROWS",
        {
            model_id: {
                "id": model_id,
                "upstream_id": "kat-coder-pro-v2",
                "display_name": "KAT Coder Pro V2",
            }
        },
    )
    result = ProviderPricingResult(
        slug="streamlake",
        prices={model_id: ModelPrice(300_000, 1_200_000)},
        source="html",
        fetched_url=streamlake.URL,
    )

    monkeypatch.setattr(streamlake, "_LIVE_CANARY_OK", False)
    streamlake.write_provider_manifest(result)
    dark = json.loads(manifest_path.read_text())["models"][0]
    assert dark["routable"] is False
    assert dark["routable_reason"] == "provider-canary-failed"

    monkeypatch.setattr(streamlake, "_LIVE_CANARY_OK", True)
    streamlake.write_provider_manifest(result)
    healthy = json.loads(manifest_path.read_text())["models"][0]
    assert "routable" not in healthy
    assert "routable_reason" not in healthy


def test_canary_state_preserves_unrelated_operator_holds(tmp_path: Path) -> None:
    manifest_path = tmp_path / "provider.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "example",
                "models": [
                    {
                        "id": "example/held",
                        "routable": False,
                        "routable_reason": "operator-hold",
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    set_manifest_canary_state(manifest_path, healthy=False)
    set_manifest_canary_state(manifest_path, healthy=True)

    row = json.loads(manifest_path.read_text())["models"][0]
    assert row["routable"] is False
    assert row["routable_reason"] == "operator-hold"


def test_wave2_hourly_refresh_and_secret_wiring_are_complete() -> None:
    assert {"inceptron", "morph", "atlas_cloud", "streamlake"}.issubset(
        PROVIDER_SLUGS
    )
    assert _PRICING_RESULT_PROVIDER_ALIASES["atlas_cloud"] == ("atlas-cloud",)

    secrets = (ROOT / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    workflow = (ROOT / ".github/workflows/refresh-prices.yml").read_text(
        encoding="utf-8"
    )
    for env_name, secret_name in {
        "INCEPTRON_API_KEY": "trustedrouter-inceptron-api-key",
        "MORPH_API_KEY": "trustedrouter-morph-api-key",
        "ATLAS_CLOUD_API_KEY": "trustedrouter-atlas-cloud-api-key",
        "STREAMLAKE_API_KEY": "trustedrouter-streamlake-api-key",
    }.items():
        assert f'ensure_secret_from_env_file "{env_name}" "{secret_name}"' in secrets
        assert f'grant_tr_deploy_secret_access "{secret_name}"' in secrets
        assert f"{env_name}:{secret_name}" in workflow
