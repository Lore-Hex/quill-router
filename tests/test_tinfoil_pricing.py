from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.pricing.providers import tinfoil
from trusted_router import provider_lifecycle
from trusted_router.catalog import MODEL_ENDPOINTS, MODELS, PROVIDERS


def test_tinfoil_fetch_ingests_glm_52_cached_input_price(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        provider_lifecycle,
        "_utc_now",
        lambda: datetime(2026, 9, 9, 23, 59, 59, tzinfo=UTC),
    )
    payload = {
        "data": [
            {
                "id": "glm-5-2",
                "pricing": {
                    "inputTokenPricePer1M": 1.5,
                    "cachedInputTokenPricePer1M": 0.375,
                    "outputTokenPricePer1M": 5.25,
                },
            },
            {
                "id": "gemma4-31b",
                "pricing": {
                    "inputTokenPricePer1M": 0.4,
                    "outputTokenPricePer1M": 1.0,
                },
            },
        ]
    }
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _url: payload)

    result = tinfoil.fetch()
    glm = result.prices["z-ai/glm-5.2"]
    gemma = result.prices["google/gemma-4-31b-it"]

    assert glm.prompt_micro_per_m == 1_500_000
    assert glm.completion_micro_per_m == 5_250_000
    assert glm.tiers[0].prompt_cached_micro_per_m == 375_000
    assert gemma.tiers[0].prompt_cached_micro_per_m is None


def test_tinfoil_fetch_discovers_glm_53_variants(monkeypatch, tmp_path: Path) -> None:  # noqa: ANN001
    manifest_path = tmp_path / "tinfoil.json"
    manifest_path.write_text(
        json.dumps({"provider": "tinfoil", "models": []}),
        encoding="utf-8",
    )
    payload = {
        "data": [
            {
                "id": "glm-5-3",
                "name": "GLM-5.3",
                "type": "chat",
                "context_window": 1_048_576,
                "endpoints": ["/v1/chat/completions", "/v1/responses"],
                "reasoning": True,
                "tool_calling": True,
                "multimodal": False,
                "pricing": {
                    "inputTokenPricePer1M": 1.8,
                    "cachedInputTokenPricePer1M": 0.45,
                    "outputTokenPricePer1M": 5.75,
                },
            },
            {
                "id": "glm-5-3-flash",
                "name": "GLM-5.3 Flash",
                "type": "chat",
                "context_window": 1_048_576,
                "endpoints": ["/v1/chat/completions", "/v1/responses"],
                "reasoning": True,
                "tool_calling": True,
                "multimodal": True,
                "pricing": {
                    "inputTokenPricePer1M": 0.4,
                    "cachedInputTokenPricePer1M": 0.1,
                    "outputTokenPricePer1M": 1.25,
                },
            },
        ]
    }
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _url: payload)
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", manifest_path)

    result = tinfoil.fetch()

    glm = result.prices["z-ai/glm-5.3"]
    flash = result.prices["z-ai/glm-5.3-flash"]
    assert (glm.prompt_micro_per_m, glm.completion_micro_per_m) == (1_800_000, 5_750_000)
    assert glm.tiers[0].prompt_cached_micro_per_m == 450_000
    assert (flash.prompt_micro_per_m, flash.completion_micro_per_m) == (400_000, 1_250_000)
    assert flash.tiers[0].prompt_cached_micro_per_m == 100_000
    assert tinfoil.UPSTREAM_ID_MAP["z-ai/glm-5.3"] == "glm-5-3"
    assert tinfoil.UPSTREAM_ID_MAP["z-ai/glm-5.3-flash"] == "glm-5-3-flash"
    glm_row = tinfoil._DISCOVERED_MANIFEST_ROWS["z-ai/glm-5.3"]
    flash_row = tinfoil._DISCOVERED_MANIFEST_ROWS["z-ai/glm-5.3-flash"]
    assert glm_row["context_length"] == 1_048_576
    assert glm_row["features"] == ["reasoning", "function-calling"]
    assert glm_row["endpoints"] == ["chat/completions", "responses"]
    assert glm_row["input_modalities"] == ["text"]
    assert flash_row["input_modalities"] == ["text", "image"]


def test_tinfoil_safely_auto_discovers_future_glm_versions(
    monkeypatch,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "tinfoil.json"
    manifest_path.write_text(
        json.dumps({"provider": "tinfoil", "models": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        tinfoil,
        "fetch_json",
        lambda _url: {
            "data": [
                {
                    "id": "glm-5-4-flash",
                    "name": "GLM-5.4 Flash",
                    "type": "chat",
                    "context_window": 1_048_576,
                    "endpoints": ["/v1/chat/completions"],
                    "pricing": {
                        "inputTokenPricePer1M": 0.5,
                        "outputTokenPricePer1M": 1.5,
                    },
                }
            ]
        },
    )

    result = tinfoil.fetch()

    assert "z-ai/glm-5.4-flash" in result.prices
    assert tinfoil.UPSTREAM_ID_MAP["z-ai/glm-5.4-flash"] == "glm-5-4-flash"
    assert "z-ai/glm-5.4-flash" in tinfoil._DISCOVERED_MANIFEST_ROWS


def test_tinfoil_review_gates_unknown_ids_and_deployment_suffixes(
    monkeypatch,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "tinfoil.json"
    manifest_path.write_text(
        json.dumps({"provider": "tinfoil", "models": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", manifest_path)
    monkeypatch.setattr(
        tinfoil,
        "fetch_json",
        lambda _url: {
            "data": [
                {
                    "id": native_id,
                    "type": "chat",
                    "endpoints": ["/v1/chat/completions"],
                    "pricing": {
                        "inputTokenPricePer1M": 0.5,
                        "outputTokenPricePer1M": 1.5,
                    },
                }
                for native_id in ("glm-5-4-fp8", "glm-5-4-0930", "unknown-model")
            ]
        },
    )

    result = tinfoil.fetch()

    assert not result.prices
    assert not tinfoil._DISCOVERED_MANIFEST_ROWS
    assert {note for note in result.notes if note.startswith("unmapped native id:")} == {
        "unmapped native id: glm-5-4-fp8",
        "unmapped native id: glm-5-4-0930",
        "unmapped native id: unknown-model",
    }


def test_tinfoil_fetch_discovers_deepseek_v4_flash(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "deepseek-v4-flash",
                "pricing": {
                    "inputTokenPricePer1M": 0.2,
                    "cachedInputTokenPricePer1M": 0.02,
                    "outputTokenPricePer1M": 0.4,
                },
            },
            {
                "id": "glm-5-2",
                "pricing": {
                    "inputTokenPricePer1M": 1.5,
                    "outputTokenPricePer1M": 5.25,
                },
            },
            {
                "id": "gemma4-31b",
                "pricing": {
                    "inputTokenPricePer1M": 0.4,
                    "outputTokenPricePer1M": 1.0,
                },
            },
        ]
    }
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _url: payload)

    result = tinfoil.fetch()
    deepseek = result.prices["deepseek/deepseek-v4-flash"]

    assert tinfoil.UPSTREAM_ID_MAP["deepseek/deepseek-v4-flash"] == ("deepseek-v4-flash")
    assert deepseek.prompt_micro_per_m == 200_000
    assert deepseek.completion_micro_per_m == 400_000
    assert deepseek.tiers[0].prompt_cached_micro_per_m == 20_000
    assert not any("deepseek/deepseek-v4-flash" in note for note in result.notes)


def test_tinfoil_manifest_writer_publishes_discovered_chat_metadata(
    monkeypatch,  # noqa: ANN001
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "tinfoil.json"
    manifest_path.write_text(
        json.dumps({"provider": "tinfoil", "models": []}),
        encoding="utf-8",
    )
    payload = {
        "data": [
            {
                "id": "deepseek-v4-flash",
                "name": "DeepSeek V4 Flash",
                "type": "chat",
                "context_window": 1_048_576,
                "endpoints": ["/v1/chat/completions", "/v1/responses"],
                "reasoning": True,
                "tool_calling": True,
                "multimodal": False,
                "pricing": {
                    "inputTokenPricePer1M": "0.20",
                    "cachedInputTokenPricePer1M": "0.02",
                    "outputTokenPricePer1M": "0.40",
                },
            }
        ]
    }
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _url: payload)
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", manifest_path)

    result = tinfoil.fetch()
    notes = tinfoil.write_provider_manifest(result)
    written = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert notes == ["tinfoil: refreshed provider_models/tinfoil.json (1 priced rows, appended 1)"]
    assert written["model_count"] == 1
    assert written["price_scale"] == "microdollars_per_million"
    assert written["models"] == [
        {
            "display_name": "DeepSeek V4 Flash",
            "title": "deepseek/deepseek-v4-flash",
            "model_type": "chat",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "endpoints": ["chat/completions", "responses"],
            "status": 1,
            "id": "deepseek/deepseek-v4-flash",
            "upstream_id": "deepseek-v4-flash",
            "features": ["reasoning", "function-calling"],
            "context_length": 1_048_576,
            "input_token_price_per_m": 200_000,
            "output_token_price_per_m": 400_000,
            "cached_input_token_price_per_m": 20_000,
        }
    ]


def test_tinfoil_deepseek_route_is_confidential_and_uses_live_prices() -> None:
    endpoint = MODEL_ENDPOINTS["deepseek/deepseek-v4-flash@tinfoil/prepaid"]
    provider = PROVIDERS["tinfoil"]

    assert endpoint.upstream_id == "deepseek-v4-flash"
    # Accepted against Tinfoil's authoritative /v1/models feed on
    # 2026-08-21: $0.30 input, $0.06 cached input, and $0.70 output.
    # Customer prices below include TrustedRouter's 5.5% markup.
    assert endpoint.prompt_price_microdollars_per_million_tokens == 316_500
    assert endpoint.completion_price_microdollars_per_million_tokens == 738_500
    assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 63_300
    assert provider.provider_zero_data_retention is True
    assert provider.provider_confidential_compute is True
    assert provider.provider_e2ee is True


def test_tinfoil_kimi_k3_route_is_confidential_and_uses_live_capabilities() -> None:
    endpoint = MODEL_ENDPOINTS["moonshotai/kimi-k3@tinfoil/prepaid"]
    provider = PROVIDERS["tinfoil"]

    assert endpoint.upstream_id == "kimi-k3"
    assert endpoint.prompt_price_microdollars_per_million_tokens == 4_220_000
    assert endpoint.completion_price_microdollars_per_million_tokens == 21_100_000
    assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 844_000
    assert provider.provider_zero_data_retention is True
    assert provider.provider_confidential_compute is True
    assert provider.provider_e2ee is True


def test_tinfoil_glm53_routes_use_live_prices_and_capabilities() -> None:
    glm = MODEL_ENDPOINTS["z-ai/glm-5.3@tinfoil/prepaid"]
    flash = MODEL_ENDPOINTS["z-ai/glm-5.3-flash@tinfoil/prepaid"]
    glm_model = MODELS["z-ai/glm-5.3"]
    flash_model = MODELS["z-ai/glm-5.3-flash"]
    provider = PROVIDERS["tinfoil"]

    assert glm.upstream_id == "glm-5-3"
    assert glm.prompt_price_microdollars_per_million_tokens == 1_899_000
    assert glm.completion_price_microdollars_per_million_tokens == 6_066_250
    assert glm.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 474_750
    assert flash.upstream_id == "glm-5-3-flash"
    assert flash.prompt_price_microdollars_per_million_tokens == 422_000
    assert flash.completion_price_microdollars_per_million_tokens == 1_318_750
    assert flash.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 105_500
    assert glm_model.context_length == 1_048_576
    assert glm_model.input_modalities == ("text",)
    assert flash_model.context_length == 1_048_576
    assert flash_model.input_modalities == ("text", "image")
    assert provider.provider_zero_data_retention is True
    assert provider.provider_confidential_compute is True
    assert provider.provider_e2ee is True
