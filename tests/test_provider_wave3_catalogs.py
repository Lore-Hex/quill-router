from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
from scripts.pricing.base import ModelPrice
from scripts.pricing.providers import (
    aion_labs,
    akashml,
    arcee,
    inception,
    mancer,
    nextbit,
    perceptron,
    perplexity,
    reka,
    sail_research,
    sambanova,
    upstage,
)
from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
    positive_chat_prices,
)
from scripts.pricing.refresh import _PRICING_RESULT_PROVIDER_ALIASES, PROVIDER_SLUGS
from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    PROVIDERS,
)
from trusted_router.services.inference_errors import default_provider_secret_ref

READY = {
    "upstage",
    "sail-research",
    "reka",
    "nextbit",
    "akashml",
    "mancer",
    "aion-labs",
    "sambanova",
    "arcee",
    "inception",
}
PENDING = {
    "perceptron",
    "perplexity",
    "sakana",
    "krea",
    "modal",
    "byteplus",
    "riverflow",
    "io-net",
    "liquid",
}
MODULES = (
    upstage,
    sail_research,
    reka,
    nextbit,
    akashml,
    mancer,
    aion_labs,
    sambanova,
    arcee,
    inception,
)


def test_wave3_ready_and_pending_providers_are_fail_closed() -> None:
    assert READY <= GATEWAY_PREPAID_PROVIDER_SLUGS
    assert PENDING.isdisjoint(GATEWAY_PREPAID_PROVIDER_SLUGS)
    for slug in READY:
        assert PROVIDERS[slug].supports_prepaid is True
        assert PROVIDERS[slug].supports_byok is False
    for slug in PENDING:
        assert PROVIDERS[slug].supports_prepaid is False
        assert PROVIDERS[slug].supports_byok is False


def test_wave3_manifests_publish_only_canaried_priced_chat_routes() -> None:
    endpoint_providers = {endpoint.provider for endpoint in MODEL_ENDPOINTS.values()}
    assert READY <= endpoint_providers
    for module in MODULES:
        manifest = json.loads(module.MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest["provider"] == module.SLUG
        assert manifest["models"]
        for row in manifest["models"]:
            assert row["upstream_id"]
            if row.get("routable") is False:
                reason = row.get("routable_reason")
                assert reason in {"provider-canary-failed", "delisted-upstream"}
                if reason == "delisted-upstream":
                    assert row.get("missing_since")
                continue
            assert row["input_token_price_per_m"] > 0
            assert row["output_token_price_per_m"] > 0


def test_failed_live_canaries_stay_dark() -> None:
    nextbit_rows = {
        row["id"]: row
        for row in json.loads(nextbit.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    }
    samba_rows = {
        row["id"]: row
        for row in json.loads(sambanova.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
    }
    assert nextbit_rows["google/gemma-2-27b-it"]["routable"] is False
    assert samba_rows["minimax/minimax-m3"]["routable"] is False


def test_upstage_parser_reads_all_three_first_party_price_axes() -> None:
    cards = "".join(
        f"""
        <div class="pricing-card-v2"><h4>{name}</h4>
          <div class="pricing-feature-v2">Input ${prompt} / 1M tokens</div>
          <div class="pricing-feature-v2">Input(Cached) ${cached} / 1M tokens</div>
          <div class="pricing-feature-v2">Output ${completion} / 1M tokens</div>
        </div>
        """
        for name, prompt, cached, completion in (
            ("Solar Pro 2", "0.15", "0.015", "0.60"),
            ("Solar Pro 3", "0.15", "0.015", "0.60"),
            ("Solar Pro 4", "0.30", "0.06", "1.20"),
        )
    )
    assert upstage._parse_pricing(cards)["upstage/solar-pro4"] == ModelPrice(
        300_000,
        1_200_000,
        prompt_cached_micro_per_m=60_000,
    )


def test_reka_parser_keeps_family_price_on_the_documented_family() -> None:
    source = """
    <table>
      <tr><td>Reka Edge</td><td>$0.10</td><td>$0.10</td></tr>
      <tr><td>Reka Flash</td><td>$0.80</td><td>$2.00</td></tr>
      <tr><td>Reka Core</td><td>$2.00</td><td>$6.00</td></tr>
    </table>
    """
    prices = reka._parse_pricing(source)
    assert prices["reka/reka-edge"] == ModelPrice(100_000, 100_000)
    assert "reka/reka-edge-2603" not in prices


def test_reka_loader_expands_only_the_explicit_version_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    source = """
    <table>
      <tr><td>Reka Edge</td><td>$0.10</td><td>$0.10</td></tr>
      <tr><td>Reka Flash</td><td>$0.80</td><td>$2.00</td></tr>
    </table>
    """
    monkeypatch.setattr(reka, "fetch_html", lambda _url: source)
    prices = reka._load_prices()
    assert prices["reka/reka-edge-2603"] == prices["reka/reka-edge"]


def test_reka_parser_reads_the_first_party_markdown_feed() -> None:
    source = r"""
    | Model | Input <br />Tokens <br />(per 1M) | Output <br />Tokens <br />(per 1M) |
    | --- | --- | --- |
    | <b>Reka Edge</b><br /><i>Compact</i> | \$0.10 | \$0.10 |
    | <b>Reka Flash</b><br /><i>Fast</i> | \$0.80 | \$2.00 |
    """
    prices = reka._parse_pricing(source)
    assert prices["reka/reka-edge"] == ModelPrice(100_000, 100_000)
    assert prices["reka/reka-flash"] == ModelPrice(800_000, 2_000_000)


def test_sail_parser_uses_asap_not_discounted_completion_windows() -> None:
    groups = "".join(
        f"""
        <tbody data-model="{native}">
          <tr>
            <td data-axis="Input" data-window="asap">${prompt}</td>
            <td data-axis="Cached" data-window="asap">${cached}</td>
            <td data-axis="Output" data-window="asap">${completion}</td>
          </tr>
        </tbody>
        """
        for native, prompt, cached, completion in (
            ("zai-org/GLM-5.2-FP8", "0.80", "0.16", "3.00"),
            ("deepseek/deepseek-v4-flash-0731", "0.09", "0.02", "0.18"),
            ("moonshotai/Kimi-K2.6", "1.00", "0.20", "4.00"),
            ("openai/gpt-oss-120b", "0.06", "0.03", "0.40"),
            ("google/gemma-4-31B-it", "0.40", "0.20", "0.60"),
            ("nvidia/Gemma-4-31B-IT-NVFP4", "0.14", "0.07", "0.40"),
        )
    )
    prices = sail_research._parse_pricing(f"<table>{groups}</table>")
    assert prices["deepseek/deepseek-v4-flash-0731"] == ModelPrice(
        90_000,
        180_000,
        prompt_cached_micro_per_m=20_000,
    )


def test_mancer_parser_uses_live_token_credits_and_least_discounted_pack() -> None:
    rows = "".join(
        f"<tr><td>{name}</td><td>context</td><td>{prompt} {completion}</td></tr>"
        for name, prompt, completion in (
            ("DeepSeek V4 Flash 0731", "0.058", "0.180"),
            ("DeepSeek V4 Flash", "0.056", "0.180"),
            ("Gemma 4 31B Instruct", "0.080", "0.400"),
            ("GLM 4.7", "0.240", "1.000"),
            ("GPT OSS 120B", "0.034", "0.200"),
            ("MythoMax", "0.160", "0.240"),
            ("ReMM-SLerp", "0.180", "0.260"),
        )
    )
    packs = [
        {"can_purchase": True, "price": "4.99", "credits": 1_250_000},
        {"can_purchase": True, "price": "19.99", "credits": 5_500_000},
    ]
    prices = mancer._parse_pricing(f"<table>{rows}</table>", packs)
    assert prices["deepseek/deepseek-v4-flash-0731"] == ModelPrice(
        231_536,
        718_560,
    )


def test_direct_provider_never_falls_back_when_live_price_loader_is_empty() -> None:
    catalog = DirectOpenAIProvider(
        DirectOpenAIProviderSpec(
            slug="empty-price-test",
            base_url="https://example.invalid/v1",
            api_key_env="EMPTY_PRICE_TEST_API_KEY",
            explicit_model_map={},
            price_loader=dict,
        ),
        manifest_path=Path("unused.json"),
    )

    with pytest.raises(RuntimeError, match="price loader returned no prices"):
        catalog._joined_prices()


def test_direct_provider_cannot_write_a_manifest_before_a_successful_fetch() -> None:
    catalog = DirectOpenAIProvider(
        DirectOpenAIProviderSpec(
            slug="unfetched-test",
            base_url="https://example.invalid/v1",
            api_key_env="UNFETCHED_TEST_API_KEY",
            explicit_model_map={},
            static_prices={"vendor/model": ModelPrice(1, 1)},
        ),
        manifest_path=Path("unused.json"),
    )
    with pytest.raises(RuntimeError, match="fetch must succeed"):
        catalog.write_provider_manifest(
            type("Result", (), {})()  # type: ignore[arg-type]
        )


def test_direct_provider_drops_any_zero_direction_before_canary() -> None:
    assert positive_chat_prices(
        {
            "ok/model": ModelPrice(1, 2),
            "zero/input": ModelPrice(0, 2),
            "zero/output": ModelPrice(1, 0),
        }
    ) == {"ok/model": ModelPrice(1, 2)}


def test_direct_provider_normalization_is_the_single_audit_policy() -> None:
    cases = {
        arcee: {
            "trinity-large-thinking": "arcee-ai/trinity-large-thinking",
            "deepseek/deepseek-v4-flash-latest": "deepseek/deepseek-v4-flash",
        },
        mancer: {"future-model": "mancer/future-model"},
        reka: {"reka-edge-2603": "reka/reka-edge-2603"},
        inception: {"mercury-2": "inception/mercury-2"},
    }
    audit_normalizers = {
        slug: normalize
        for slug, _url, _env_names, normalize in _DISCOVERABLE_MANIFEST_PROVIDERS
    }
    for module, native_cases in cases.items():
        for native_id, expected in native_cases.items():
            assert module.CATALOG.model_id(native_id) == expected
            assert audit_normalizers[module.SLUG](native_id) == expected


def test_price_parsers_survive_documented_model_retirement() -> None:
    upstage_source = """
      <div class="pricing-card-v2"><h4>Solar Pro 4</h4>
        <div class="pricing-feature-v2">Input $0.30 / 1M tokens</div>
        <div class="pricing-feature-v2">Input(Cached) $0.06 / 1M tokens</div>
        <div class="pricing-feature-v2">Output $1.20 / 1M tokens</div>
      </div>
    """
    assert set(upstage._parse_pricing(upstage_source)) == {"upstage/solar-pro4"}

    sail_source = """
      <table><tbody data-model="zai-org/GLM-5.2-FP8"><tr>
        <td data-axis="Input" data-window="asap">$0.80</td>
        <td data-axis="Cached" data-window="asap">$0.16</td>
        <td data-axis="Output" data-window="asap">$3.00</td>
      </tr></tbody></table>
    """
    assert set(sail_research._parse_pricing(sail_source)) == {"z-ai/glm-5.2"}

    mancer_source = """
      <table><tr><td>GLM 4.7</td><td>context</td><td>0.240 1.000</td></tr></table>
    """
    packs = [{"can_purchase": True, "price": "4.99", "credits": 1_250_000}]
    assert set(mancer._parse_pricing(mancer_source, packs)) == {"z-ai/glm-4.7"}


def test_perplexity_converts_catalog_usd_per_million_without_floats() -> None:
    rows = perplexity._normalize_rows(
        [
            {
                "id": "perplexity/sonar",
                "pricing": {
                    "unit": "usd_per_1m_tokens",
                    "input": "1.00",
                    "output": "2.00",
                    "cache_read": "0.10",
                },
            }
        ]
    )
    assert rows[0]["pricing"] == {
        "prompt": str(Decimal("0.000001")),
        "completion": str(Decimal("0.000002")),
        "input_cache_read": str(Decimal("0.0000001")),
    }


def test_perceptron_filters_media_free_and_unpriced_rows() -> None:
    rows = perceptron._normalize_rows(
        [
            {
                "id": "ok",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "pricing": {"input_price_per_1m": "1", "output_price_per_1m": "2"},
            },
            {
                "id": "image",
                "input_modalities": ["text"],
                "output_modalities": ["image"],
                "pricing": {"input_price_per_1m": "1", "output_price_per_1m": "2"},
            },
            {
                "id": "free",
                "is_free": True,
                "pricing": {"input_price_per_1m": "1", "output_price_per_1m": "2"},
            },
            {
                "id": "zero",
                "pricing": {"input_price_per_1m": "0", "output_price_per_1m": "0"},
            },
            {
                "id": "zero-input-only",
                "pricing": {"input_price_per_1m": "0", "output_price_per_1m": "2"},
            },
            {
                "id": "zero-output-only",
                "pricing": {"input_price_per_1m": "1", "output_price_per_1m": "0"},
            },
        ]
    )
    assert [row["id"] for row in rows] == ["ok"]


def test_wave3_hourly_discovery_and_provider_aliases_are_registered() -> None:
    module_names = {slug.replace("-", "_") for slug in READY}
    assert module_names <= set(PROVIDER_SLUGS)
    assert _PRICING_RESULT_PROVIDER_ALIASES["sail_research"] == ("sail-research",)
    assert _PRICING_RESULT_PROVIDER_ALIASES["aion_labs"] == ("aion-labs",)
    discoverable = {slug for slug, _url, _env, _normalize in _DISCOVERABLE_MANIFEST_PROVIDERS}
    assert READY <= discoverable


def test_hyphenated_provider_secret_refs_use_valid_environment_names() -> None:
    assert default_provider_secret_ref("sail-research") == "env://SAIL_RESEARCH_API_KEY"
    assert default_provider_secret_ref("aion-labs") == "env://AION_LABS_API_KEY"
