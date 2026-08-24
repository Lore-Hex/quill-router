from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from scripts.check_price_coverage import _DISCOVERABLE_MANIFEST_PROVIDERS
from scripts.pricing.base import ModelPrice
from scripts.pricing.openai_catalog import discover_available_priced_chat_catalog
from scripts.pricing.providers import (
    _direct_openai,
    aion_labs,
    akashml,
    arcee,
    inception,
    io_net,
    mancer,
    nextbit,
    perceptron,
    perplexity,
    reka,
    sail_research,
    sakana,
    sambanova,
    upstage,
)
from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
    positive_chat_prices,
)
from scripts.pricing.refresh import _PRICING_RESULT_PROVIDER_ALIASES, PROVIDER_SLUGS
from trusted_router import catalog_ingest
from trusted_router.catalog import (
    GATEWAY_PREPAID_PROVIDER_SLUGS,
    MODEL_ENDPOINTS,
    PROVIDERS,
    endpoints_for_model,
)
from trusted_router.catalog_ingest import (
    _EXPIRED_PROVIDER_MANIFEST,
    _RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS,
    _provider_manifest_valid_until,
)
from trusted_router.pricing import (
    provider_manifest_price_profile_is_valid,
    provider_manifest_price_tiers_are_valid,
)
from trusted_router.provider_manifest_policy import (
    EXPIRING_PROVIDER_MANIFEST_SLUGS,
    PROVIDER_MANIFEST_MAX_AGE_DAYS,
    RUNTIME_ONLY_PROVIDER_MANIFEST_MAX_AGE_DAYS,
    RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS,
)
from trusted_router.services.inference_errors import default_provider_secret_ref

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "provider_wave3"

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
    "io-net",
    "sakana",
}
RUNTIME_ONLY_READY = READY - {"io-net", "sakana"}
ROUTABLE_READY = READY - {"sakana"}
PENDING = {
    "perceptron",
    "perplexity",
    "krea",
    "modal",
    "byteplus",
    "riverflow",
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
    io_net,
    sakana,
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
    assert ROUTABLE_READY <= endpoint_providers
    assert "sakana" not in endpoint_providers
    for module in MODULES:
        manifest = json.loads(module.MANIFEST_PATH.read_text(encoding="utf-8"))
        assert manifest["provider"] == module.SLUG
        assert manifest["models"]
        for row in manifest["models"]:
            assert row["upstream_id"]
            if row.get("routable") is False:
                reason = row.get("routable_reason")
                assert reason in {
                    "provider-canary-failed",
                    "delisted-upstream",
                    "unbounded-provider-orchestration-cost",
                    "provider-geographic-restriction",
                }
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
    assert not any(
        endpoint.provider == "nextbit"
        and endpoint.model_id == "google/gemma-2-27b-it"
        for endpoint in MODEL_ENDPOINTS.values()
    )
    assert not any(
        endpoint.provider == "sambanova"
        and endpoint.model_id == "minimax/minimax-m3"
        for endpoint in MODEL_ENDPOINTS.values()
    )


def test_upstage_parser_reads_all_three_first_party_price_axes() -> None:
    source = (FIXTURE_DIR / "upstage.html").read_text(encoding="utf-8")
    assert upstage._parse_pricing(source)["upstage/solar-pro4"] == ModelPrice(
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
    source = (FIXTURE_DIR / "reka.md").read_text(encoding="utf-8")
    prices = reka._parse_pricing(source)
    assert prices["reka/reka-edge"] == ModelPrice(100_000, 100_000)
    assert prices["reka/reka-flash"] == ModelPrice(800_000, 2_000_000)


def test_sail_parser_uses_asap_not_discounted_completion_windows() -> None:
    source = (FIXTURE_DIR / "sail-research.html").read_text(encoding="utf-8")
    prices = sail_research._parse_pricing(source)
    assert prices["deepseek/deepseek-v4-flash-0731"] == ModelPrice(
        90_000,
        180_000,
        prompt_cached_micro_per_m=20_000,
    )


def test_mancer_parser_uses_live_token_credits_and_least_discounted_pack() -> None:
    source = (FIXTURE_DIR / "mancer.html").read_text(encoding="utf-8")
    packs = json.loads((FIXTURE_DIR / "mancer-credit-packs.json").read_text())
    prices = mancer._parse_pricing(source, packs)
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


def test_price_joined_catalog_excludes_non_text_output_models() -> None:
    upstream_ids: dict[str, str] = {}
    discovered = discover_available_priced_chat_catalog(
        [
            {"id": "vendor/text", "output_modalities": ["text"]},
            {"id": "vendor/image", "output_modalities": ["image"]},
        ],
        prices={
            "vendor/text": ModelPrice(1, 2),
            "vendor/image": ModelPrice(1, 2),
        },
        explicit_map={
            "vendor/text": "vendor/text",
            "vendor/image": "vendor/image",
        },
        upstream_id_map=upstream_ids,
    )

    assert set(discovered) == {"vendor/text"}
    assert upstream_ids == {"vendor/text": "vendor/text"}


def test_direct_provider_catalog_fetch_uses_shared_retry_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_fetch_json(url: str, *, extra_headers: dict[str, str]) -> object:
        seen["url"] = url
        seen["headers"] = extra_headers
        return {"data": [{"id": "vendor/model"}]}

    monkeypatch.setenv("SHARED_FETCH_TEST_API_KEY", "test-secret")
    monkeypatch.setattr(_direct_openai, "fetch_json", fake_fetch_json)
    monkeypatch.setattr(_direct_openai, "models_requiring_canary", lambda *_args: set())
    catalog = DirectOpenAIProvider(
        DirectOpenAIProviderSpec(
            slug="shared-fetch-test",
            base_url="https://example.invalid/v1",
            api_key_env="SHARED_FETCH_TEST_API_KEY",
            explicit_model_map={"vendor/model": "vendor/model"},
            static_prices={"vendor/model": ModelPrice(1, 2)},
        ),
        manifest_path=Path("unused.json"),
    )

    result = catalog.fetch()

    assert result.prices == {"vendor/model": ModelPrice(1, 2)}
    assert seen == {
        "url": "https://example.invalid/v1/models",
        "headers": {"Authorization": "Bearer test-secret"},
    }


def test_direct_provider_operator_hold_survives_canary_and_relist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "held.json"
    catalog_rows = [{"id": "vendor/held"}, {"id": "vendor/live"}]
    probed: list[str] = []
    manifest.write_text(
        json.dumps(
            {
                "provider": "held-provider",
                "generated_at": "2026-08-01T00:00:00Z",
                "models": [
                    {
                        "id": model_id,
                        "upstream_id": model_id,
                        "routable": True,
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 2,
                    }
                    for model_id in ("vendor/held", "vendor/live")
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HELD_PROVIDER_API_KEY", "test-secret")
    monkeypatch.setattr(
        _direct_openai,
        "fetch_json",
        lambda *_args, **_kwargs: {"data": list(catalog_rows)},
    )
    monkeypatch.setattr(
        _direct_openai,
        "models_requiring_canary",
        lambda _path, model_ids: set(model_ids),
    )

    def probe(**kwargs: object) -> bool:
        probed.append(str(kwargs["model"]))
        return True

    monkeypatch.setattr(_direct_openai, "probe_openai_chat", probe)
    catalog = DirectOpenAIProvider(
        DirectOpenAIProviderSpec(
            slug="held-provider",
            base_url="https://example.invalid/v1",
            api_key_env="HELD_PROVIDER_API_KEY",
            explicit_model_map={
                "vendor/held": "vendor/held",
                "vendor/live": "vendor/live",
            },
            static_prices={
                "vendor/held": ModelPrice(1, 2),
                "vendor/live": ModelPrice(1, 2),
            },
            operator_hold_reasons={"vendor/held": "operator-hold"},
        ),
        manifest_path=manifest,
    )

    def refresh() -> dict[str, dict[str, object]]:
        result = catalog.fetch()
        catalog.write_provider_manifest(result)
        return {
            row["id"]: row
            for row in json.loads(manifest.read_text(encoding="utf-8"))["models"]
        }

    assert refresh()["vendor/held"]["routable_reason"] == "operator-hold"
    first_refresh = json.loads(manifest.read_text(encoding="utf-8"))
    assert first_refresh["generated_at"] == "2026-08-01T00:00:00Z"
    catalog_rows[:] = [{"id": "vendor/live"}]
    assert refresh()["vendor/held"]["routable_reason"] == "operator-hold"
    assert refresh()["vendor/held"]["routable_reason"] == "operator-hold"
    catalog_rows[:] = [{"id": "vendor/held"}, {"id": "vendor/live"}]
    relisted = refresh()["vendor/held"]

    assert relisted["routable"] is False
    assert relisted["routable_reason"] == "operator-hold"
    assert "missing_since" not in relisted
    assert probed == ["vendor/live"] * 4


def test_direct_provider_normalization_is_the_single_audit_policy() -> None:
    cases = {
        arcee: {
            "trinity-large-thinking": "arcee-ai/trinity-large-thinking",
            "deepseek/deepseek-v4-flash-latest": "deepseek/deepseek-v4-flash",
        },
        mancer: {"future-model": None},
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
      <table>
        <thead><tr><th>Model</th><th>Context</th><th>Price (credits)</th></tr></thead>
        <tr><td>GLM 4.7</td><td>context</td><td>
          <x-intag>0.240</x-intag><x-outtag>1.000</x-outtag>
        </td></tr>
      </table>
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


def test_perplexity_uses_distinct_catalog_and_inference_paths() -> None:
    assert perplexity.BASE_URL == "https://api.perplexity.ai"
    assert perplexity.URL == "https://api.perplexity.ai/v1/models"


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


def test_io_net_normalizes_exact_prices_capabilities_and_limits() -> None:
    rows = io_net._normalize_rows(
        [
            {
                "id": "XiaomiMiMo/MiMo-V2.5",
                "input_token_price": "0.0000001934",
                "output_token_price": "0.0000006268",
                "cache_read_token_price": "0.0000000967",
                "context_window": 262144,
                "max_tokens": 32768,
                "supports_tools": True,
                "supports_reasoning": True,
                "supports_prompt_cache": True,
            }
        ]
    )

    assert rows == [
        {
            "id": "XiaomiMiMo/MiMo-V2.5",
            "input_token_price": "0.0000001934",
            "output_token_price": "0.0000006268",
            "cache_read_token_price": "0.0000000967",
            "context_window": 262144,
            "max_tokens": 32768,
            "supports_tools": True,
            "supports_reasoning": True,
            "supports_prompt_cache": True,
            "pricing": {
                "prompt": "1.934E-7",
                "completion": "6.268E-7",
                "input_cache_read": "9.67E-8",
            },
            "context_length": 262144,
            "max_output_tokens": 32768,
            "supported_features": ["tools", "reasoning", "prompt_cache"],
        }
    ]
    assert io_net.CATALOG.model_id("XiaomiMiMo/MiMo-V2.5") == "xiaomi/mimo-v2.5"


def test_io_net_drops_unpriced_or_one_sided_rows() -> None:
    rows = io_net._normalize_rows(
        [
            {"id": "missing"},
            {
                "id": "zero-input",
                "input_token_price": "0",
                "output_token_price": "0.5",
            },
            {
                "id": "zero-output",
                "input_token_price": "0.5",
                "output_token_price": "0",
            },
        ]
    )
    assert rows == []


def test_wave3_hourly_discovery_and_provider_aliases_are_registered() -> None:
    module_names = {slug.replace("-", "_") for slug in READY}
    assert module_names <= set(PROVIDER_SLUGS)
    assert _PRICING_RESULT_PROVIDER_ALIASES["sail_research"] == ("sail-research",)
    assert _PRICING_RESULT_PROVIDER_ALIASES["aion_labs"] == ("aion-labs",)
    assert _PRICING_RESULT_PROVIDER_ALIASES["io_net"] == ("io-net",)
    discoverable = {slug for slug, _url, _env, _normalize in _DISCOVERABLE_MANIFEST_PROVIDERS}
    assert READY <= discoverable


def test_hyphenated_provider_secret_refs_use_valid_environment_names() -> None:
    assert default_provider_secret_ref("sail-research") == "env://SAIL_RESEARCH_API_KEY"
    assert default_provider_secret_ref("aion-labs") == "env://AION_LABS_API_KEY"
    assert default_provider_secret_ref("io-net") == "env://IONET_API_KEY"


def test_unqualified_unknown_models_are_not_guessed_for_aggregators() -> None:
    assert arcee.CATALOG.model_id("future-model") is None
    assert mancer.CATALOG.model_id("future-model") is None


def test_perplexity_omits_an_undocumented_cache_rate() -> None:
    rows = perplexity._normalize_rows(
        [
            {
                "id": "perplexity/sonar",
                "pricing": {
                    "unit": "usd_per_1m_tokens",
                    "input": "1.00",
                    "output": "2.00",
                },
            }
        ]
    )
    assert "input_cache_read" not in rows[0]["pricing"]


def test_perplexity_accepts_unqualified_native_ids_for_namespacing() -> None:
    assert perplexity._is_perplexity_route({"id": "sonar"}) is True
    assert perplexity._is_perplexity_route({"id": "r1-1776"}) is True
    assert perplexity.CATALOG.model_id("sonar") == "perplexity/sonar"
    assert perplexity.CATALOG.model_id("Sonar-Pro") == "perplexity/sonar-pro"
    assert perplexity._is_perplexity_route({"id": "other/sonar"}) is False
    assert perplexity._is_perplexity_route({"id": "llama-3.1-70b-instruct"}) is False


def test_wave3_pricing_fixtures_are_captured_from_first_party_sources() -> None:
    expected_sources = {
        "upstage.html": "https://www.upstage.ai/pricing/api",
        "reka.md": "https://docs.reka.ai/pricing.md",
        "sail-research.html": "https://docs.sailresearch.com/pricing",
        "mancer.html": "https://mancer.tech/models",
    }
    for filename, source_url in expected_sources.items():
        source = (FIXTURE_DIR / filename).read_text(encoding="utf-8")
        assert f"Captured from {source_url}" in source


def test_wave3_secrets_do_not_join_the_all_or_nothing_refresh_block() -> None:
    root = Path(__file__).parents[1]
    workflow = (
        root / ".github/workflows/refresh-prices.yml"
    ).read_text(encoding="utf-8")
    secret_setup = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    runtime_only = {
        line.strip()
        for line in (
            root / "scripts/deploy/runtime_only_provider_secrets.txt"
        ).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    mandatory_step = workflow.split("- name: Pull PARASAIL_API_KEY", 1)[1]
    mandatory_step = mandatory_step.split("- name:", 1)[0]
    expected_secrets = {
        f"trustedrouter-{module.SLUG}-api-key"
        for module in MODULES
        if module.SLUG in RUNTIME_ONLY_READY
    }
    assert runtime_only == expected_secrets
    for module in MODULES:
        if module.SLUG not in RUNTIME_ONLY_READY:
            continue
        secret_name = f"trustedrouter-{module.SLUG}-api-key"
        assert secret_name not in mandatory_step
        assert f'grant_tr_deploy_secret_access "{secret_name}"' not in secret_setup

    assert _RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS == RUNTIME_ONLY_READY
    assert RUNTIME_ONLY_PROVIDER_MANIFEST_SLUGS == RUNTIME_ONLY_READY
    assert READY < EXPIRING_PROVIDER_MANIFEST_SLUGS
    assert PROVIDER_MANIFEST_MAX_AGE_DAYS == 14
    assert RUNTIME_ONLY_PROVIDER_MANIFEST_MAX_AGE_DAYS == 14


def test_runtime_only_provider_routes_expire_without_freezing_other_catalogs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sample = next(endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "upstage")
    now = datetime.now(UTC)
    expired = replace(
        sample,
        id=f"{sample.id}-expired-test",
        catalog_valid_until=now - timedelta(seconds=1),
    )
    current = replace(
        sample,
        id=f"{sample.id}-current-test",
        catalog_valid_until=now + timedelta(minutes=5),
    )
    monkeypatch.setitem(MODEL_ENDPOINTS, expired.id, expired)
    monkeypatch.setitem(MODEL_ENDPOINTS, current.id, current)

    endpoint_ids = {endpoint.id for endpoint in endpoints_for_model(sample.model_id)}
    assert expired.id not in endpoint_ids
    assert current.id in endpoint_ids
    assert sample.catalog_valid_until is not None


def test_malformed_runtime_only_manifest_expires_every_route() -> None:
    assert _provider_manifest_valid_until(
        "upstage",
        {
            "generated_at": "2026-08-22T00:00:00Z",
            "models": [
                {
                    "id": "upstage/bad-price",
                    "routable": True,
                    "input_token_price_per_m": 0,
                    "output_token_price_per_m": 100,
                }
            ],
        },
    ) == _EXPIRED_PROVIDER_MANIFEST


@pytest.mark.parametrize(
    "bad_tiers",
    [
        [],
        [
            {
                "max_prompt_tokens": None,
                "input_token_price_per_m": 100,
                "output_token_price_per_m": 200,
            },
            {
                "max_prompt_tokens": 200_000,
                "input_token_price_per_m": 200,
                "output_token_price_per_m": 400,
            },
        ],
        [
            {
                "max_prompt_tokens": 500_000,
                "input_token_price_per_m": 100,
                "output_token_price_per_m": 200,
            },
            {
                "max_prompt_tokens": 272_000,
                "input_token_price_per_m": 200,
                "output_token_price_per_m": 400,
            },
            {
                "max_prompt_tokens": None,
                "input_token_price_per_m": 300,
                "output_token_price_per_m": 600,
            },
        ],
        [
            {
                "max_prompt_tokens": 272_000,
                "input_token_price_per_m": 100,
                "output_token_price_per_m": 200,
            }
        ],
        [
            {
                "max_prompt_tokens": 272_000,
                "input_token_price_per_m": 200,
                "output_token_price_per_m": 400,
            },
            {
                "max_prompt_tokens": None,
                "input_token_price_per_m": 100,
                "output_token_price_per_m": 200,
            },
        ],
        [
            {
                "max_prompt_tokens": 272_000,
                "input_token_price_per_m": 100,
                "output_token_price_per_m": 200,
                "cached_input_token_price_per_m": 0.5,
            },
            {
                "max_prompt_tokens": None,
                "input_token_price_per_m": 200,
                "output_token_price_per_m": 400,
                "cached_input_token_price_per_m": 1,
            },
        ],
        [
            {
                "max_prompt_tokens": 272_000,
                "input_token_price_per_m": 101,
                "output_token_price_per_m": 200,
            },
            {
                "max_prompt_tokens": None,
                "input_token_price_per_m": 200,
                "output_token_price_per_m": 400,
            },
        ],
    ],
)
def test_malformed_price_tiers_expire_provider_manifest(bad_tiers: object) -> None:
    assert _provider_manifest_valid_until(
        "upstage",
        {
            "generated_at": "2026-08-22T00:00:00Z",
            "models": [
                {
                    "id": "upstage/bad-tiers",
                    "routable": True,
                    "input_token_price_per_m": 100,
                    "output_token_price_per_m": 200,
                    "price_tiers": bad_tiers,
                }
            ],
        },
    ) == _EXPIRED_PROVIDER_MANIFEST


def test_malformed_snapshot_tiers_create_no_catalog_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "google/test-tier-model",
                        "context_length": 1_000_000,
                        "endpoints": [
                            {
                                "tr_provider_slug": "google-ai-studio",
                                "model_id": "test-tier-model",
                                "context_length": 1_000_000,
                                "pricing": {
                                    "prompt": "0.000001",
                                    "completion": "0.000002",
                                    "prompt_tiers": [
                                        {
                                            "max_prompt_tokens": 500_000,
                                            "prompt": "0.000001",
                                        },
                                        {
                                            "max_prompt_tokens": 272_000,
                                            "prompt": "0.000002",
                                        },
                                        {
                                            "max_prompt_tokens": None,
                                            "prompt": "0.000003",
                                        },
                                    ],
                                    "completion_tiers": [
                                        {
                                            "max_prompt_tokens": 500_000,
                                            "completion": "0.000002",
                                        },
                                        {
                                            "max_prompt_tokens": 272_000,
                                            "completion": "0.000004",
                                        },
                                        {
                                            "max_prompt_tokens": None,
                                            "completion": "0.000006",
                                        },
                                    ],
                                },
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_ingest, "_INGEST_PATH", snapshot)

    models, endpoints = catalog_ingest._ingested_models_and_endpoints()

    assert "google/test-tier-model" not in models
    assert not any(
        endpoint.model_id == "google/test-tier-model"
        for endpoint in endpoints.values()
    )


def test_every_committed_provider_price_tier_is_structurally_safe() -> None:
    manifests = Path(__file__).parents[1] / "src/trusted_router/data/provider_models"
    for path in manifests.glob("*.json"):
        raw = json.loads(path.read_text(encoding="utf-8"))
        for row in raw.get("models", []):
            if isinstance(row, dict) and "price_tiers" in row:
                assert provider_manifest_price_tiers_are_valid(row["price_tiers"]), (
                    f"{path.name}:{row.get('id')} has unsafe price_tiers"
                )
                assert provider_manifest_price_profile_is_valid(row), (
                    f"{path.name}:{row.get('id')} headline disagrees with tier zero"
                )


def test_media_fallback_manifests_receive_provider_scoped_expiry() -> None:
    for provider_slug in ("bfl", "decart", "recraft"):
        raw = json.loads(
            (
                Path(__file__).parents[1]
                / "src/trusted_router/data/provider_models"
                / f"{provider_slug}.json"
            ).read_text(encoding="utf-8")
        )
        deadline = _provider_manifest_valid_until(provider_slug, raw)
        assert deadline is not None
        assert deadline != _EXPIRED_PROVIDER_MANIFEST

    decart_endpoints = [
        endpoint for endpoint in MODEL_ENDPOINTS.values() if endpoint.provider == "decart"
    ]
    assert decart_endpoints
    assert any(endpoint.model_id == "decart/lucy-2.5" for endpoint in decart_endpoints)
    assert all(endpoint.catalog_valid_until is not None for endpoint in decart_endpoints)


def test_malformed_media_price_expires_entire_provider_manifest() -> None:
    assert _provider_manifest_valid_until(
        "bfl",
        {
            "generated_at": "2026-08-22T00:00:00Z",
            "models": [
                {
                    "id": "black-forest-labs/bad-image-price",
                    "model_type": "image",
                    "routable": True,
                    "fixed_output_price_microdollars": {"1k": 0},
                }
            ],
        },
    ) == _EXPIRED_PROVIDER_MANIFEST


def test_free_cache_cost_is_not_mistaken_for_missing_cache_price(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import catalog_ingest

    (tmp_path / "upstage.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now(UTC).isoformat(),
                "models": [
                    {
                        "id": "upstage/cache-free-test",
                        "upstream_id": "cache-free-test",
                        "display_name": "Cache Free Test",
                        "model_type": "chat",
                        "endpoints": ["chat/completions"],
                        "routable": True,
                        "input_token_price_per_m": 1_000_000,
                        "output_token_price_per_m": 2_000_000,
                        "cached_input_token_price_per_m": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)

    _models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    endpoint = endpoints["upstage/cache-free-test@upstage/prepaid"]

    assert endpoint.prompt_price_microdollars_per_million_tokens == 1_055_000
    assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 10_000


def test_malformed_optional_cache_cost_falls_back_to_prompt_rate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trusted_router import catalog_ingest

    (tmp_path / "baseten.json").write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "baseten/malformed-cache-test",
                        "upstream_id": "malformed-cache-test",
                        "display_name": "Malformed Cache Test",
                        "model_type": "chat",
                        "endpoints": ["chat/completions"],
                        "routable": True,
                        "input_token_price_per_m": 1_000_000,
                        "output_token_price_per_m": 2_000_000,
                        "cached_input_token_price_per_m": "not-a-price",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(catalog_ingest, "_PROVIDER_MODELS_DIR", tmp_path)

    _models, endpoints = catalog_ingest._supplemental_provider_models_and_endpoints()
    endpoint = endpoints["baseten/malformed-cache-test@baseten/prepaid"]

    assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens is None


def test_every_deploy_verifies_runtime_only_provider_secret_isolation() -> None:
    root = Path(__file__).parents[1]
    workflow = (root / ".github/workflows/deploy.yml").read_text(encoding="utf-8")
    secret_setup = (root / "scripts/deploy/secrets.sh").read_text(encoding="utf-8")
    revocation = (
        root / "scripts/deploy/revoke_runtime_only_secret_access.sh"
    ).read_text(encoding="utf-8")

    assert "bash scripts/deploy/revoke_runtime_only_secret_access.sh" in workflow
    assert 'bash "${SCRIPT_DIR}/revoke_runtime_only_secret_access.sh"' not in secret_setup
    assert "Verify deploy isolation from runtime-only provider keys" in workflow
    assert "secrets get-iam-policy" in revocation
    assert "secrets remove-iam-policy-binding" in revocation
    assert "secrets versions access latest" in revocation
    assert '--role="roles/secretmanager.secretAccessor"' in revocation
    assert "still has accessor" in revocation
    assert "has effective access" in revocation
    assert 'read -r secret_name <&3' in revocation
    assert "MAX_ATTEMPTS" in revocation
    assert "NOT_FOUND|FAILED_PRECONDITION" not in revocation
    assert "secretmanager\\.versions\\.access.*denied" in revocation
    assert "TR_REMEDIATE_RUNTIME_ONLY_SECRET_IAM" in revocation
    assert "setIamPolicy" in revocation


def test_runtime_only_secret_isolation_verifier_and_operator_repair(
    tmp_path: Path,
) -> None:
    root = Path(__file__).parents[1]
    fake_gcloud = tmp_path / "gcloud"
    fake_gcloud.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$FAKE_GCLOUD_CALLS"
if printf '%s\\n' "$*" | grep -q "projects describe"; then
  echo "123456789"
  exit 0
fi
if printf '%s\\n' "$*" | grep -q "auth list"; then
  echo "${FAKE_GCLOUD_ACCOUNT:-tr-deploy@test-project.iam.gserviceaccount.com}"
  exit 0
fi
while [ "$#" -gt 0 ] && [ "$1" != "secrets" ]; do shift; done
[ "$#" -ge 3 ]
action="$2"
secret_name="$3"
if [ "$action" = "versions" ]; then
  for argument in "$@"; do
    case "$argument" in --secret=*) secret_name="${argument#--secret=}" ;; esac
  done
  state="$FAKE_GCLOUD_STATE/$secret_name"
  if [ -f "$state" ]; then
    echo "PERMISSION_DENIED: secretmanager.versions.access denied" >&2
    exit 1
  fi
  echo "supersecret-that-must-not-escape"
  exit 0
fi
state="$FAKE_GCLOUD_STATE/$secret_name"
case "$action" in
  get-iam-policy)
    if [ ! -f "$state" ]; then
      echo "serviceAccount:tr-deploy@test-project.iam.gserviceaccount.com"
    fi
    ;;
  remove-iam-policy-binding) touch "$state" ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    fake_gcloud.chmod(0o755)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    calls = tmp_path / "calls.log"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PROJECT_ID": "test-project",
        "TR_DEPLOY_SA": "tr-deploy@test-project.iam.gserviceaccount.com",
        "FAKE_GCLOUD_CALLS": str(calls),
        "FAKE_GCLOUD_STATE": str(state_dir),
    }
    script = root / "scripts/deploy/revoke_runtime_only_secret_access.sh"
    secret_names = [f"trustedrouter-{module.SLUG}-api-key" for module in MODULES]
    for secret_name in secret_names:
        state_dir.joinpath(secret_name).touch()

    subprocess.run(  # noqa: S603 - checked-in script with a fake gcloud binary
        [str(script)], cwd=root, env=env, check=True, capture_output=True
    )
    subprocess.run(  # noqa: S603 - second run proves idempotency
        [str(script)], cwd=root, env=env, check=True, capture_output=True
    )

    assert "get-iam-policy" not in calls.read_text(encoding="utf-8")
    assert "remove-iam-policy-binding" not in calls.read_text(encoding="utf-8")

    # Reintroduce one old direct binding. The deploy identity detects it but
    # cannot mutate IAM or leak the captured secret value.
    state_dir.joinpath(secret_names[0]).unlink()
    failed = subprocess.run(  # noqa: S603 - intentional fail-closed probe
        [str(script)], cwd=root, env=env, check=False, capture_output=True, text=True
    )
    assert failed.returncode != 0
    assert "has effective access" in failed.stderr
    assert "supersecret-that-must-not-escape" not in failed.stdout + failed.stderr
    assert "remove-iam-policy-binding" not in calls.read_text(encoding="utf-8")

    # An operator can remove only the direct binding. The deploy identity then
    # independently proves effective denial on the next run.
    repair_calls = tmp_path / "repair-calls.log"
    repair_env = {
        **env,
        "FAKE_GCLOUD_ACCOUNT": "operator@example.com",
        "FAKE_GCLOUD_CALLS": str(repair_calls),
        "TR_REMEDIATE_RUNTIME_ONLY_SECRET_IAM": "1",
    }
    subprocess.run(  # noqa: S603 - checked-in operator remediation path
        [str(script)], cwd=root, env=repair_env, check=True, capture_output=True
    )
    assert repair_calls.read_text(encoding="utf-8").count(
        "remove-iam-policy-binding"
    ) == 1
    assert "secrets versions access latest" not in repair_calls.read_text(
        encoding="utf-8"
    )
    subprocess.run(  # noqa: S603 - post-repair effective denial proof
        [str(script)], cwd=root, env=env, check=True, capture_output=True
    )


def test_price_coverage_failure_blocks_commit_and_deploy() -> None:
    workflow = (
        Path(__file__).parents[1] / ".github/workflows/refresh-prices.yml"
    ).read_text(encoding="utf-8")

    audit = workflow.index("- name: Price-source coverage audit")
    blocker = workflow.index("- name: Block unsafe coverage before publication")
    commit = workflow.index("- name: Commit and push if changed")
    assert audit < blocker < commit
    blocker_step = workflow[blocker:commit]
    assert "steps.coverage_audit.outcome == 'failure'" in blocker_step
    assert "exit 1" in blocker_step
    validation = workflow.index("- name: Validate generated catalog")
    validation_step = workflow[validation:commit]
    assert "if: always()" not in validation_step
    assert "continue-on-error: true" not in validation_step
    assert "last known-good snapshot remains live" in workflow
    assert "manifest_attention=true" in workflow
    assert "discovery_attention=true" in workflow
    assert "model discovery fetch failed" in workflow
    assert "returned no model ids" in workflow
    assert "found no GLM model ids" in workflow
    assert "official video price verification unavailable" in workflow
    assert '"${COVERAGE_OUTCOME}" != "success"' in workflow
    assert '"${JOB_STATUS}" != "success"' in workflow
    assert '"${MANIFEST_ATTENTION}" != "false"' in workflow
    assert '"${DISCOVERY_ATTENTION}" != "false"' in workflow
    assert "(coverage summary unavailable)" in workflow
    assert "Coverage audit incomplete" in workflow
    assert "Catalog publication incomplete" in workflow
    assert "Provider manifest expiry" in workflow
    assert "Model discovery unavailable" in workflow
    assert "provider-scoped route deadline" in workflow


def test_wave3_refreshes_reuse_committed_manifests_when_live_auth_is_unavailable() -> None:
    for module in MODULES:
        assert module.MANIFEST_STALE_FALLBACK is True
        rows = json.loads(module.MANIFEST_PATH.read_text(encoding="utf-8"))["models"]
        assert rows
