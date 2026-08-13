from __future__ import annotations

import json
from pathlib import Path

from scripts.pricing.providers import tinfoil
from trusted_router.catalog import MODEL_ENDPOINTS, PROVIDERS


def test_tinfoil_fetch_ingests_glm_52_cached_input_price(monkeypatch) -> None:  # noqa: ANN001
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


def test_tinfoil_fetch_discovers_deepseek_v4_flash(monkeypatch) -> None:  # noqa: ANN001
    payload = {
        "data": [
            {
                "id": "deepseek-v4-flash",
                "pricing": {
                    "inputTokenPricePer1M": 0.7,
                    "cachedInputTokenPricePer1M": 0.125,
                    "outputTokenPricePer1M": 1.9,
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

    assert tinfoil.UPSTREAM_ID_MAP["deepseek/deepseek-v4-flash"] == (
        "deepseek-v4-flash"
    )
    assert deepseek.prompt_micro_per_m == 700_000
    assert deepseek.completion_micro_per_m == 1_900_000
    assert deepseek.tiers[0].prompt_cached_micro_per_m == 125_000
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
                    "inputTokenPricePer1M": "0.70",
                    "cachedInputTokenPricePer1M": "0.125",
                    "outputTokenPricePer1M": "1.90",
                },
            }
        ]
    }
    monkeypatch.setattr(tinfoil, "fetch_json", lambda _url: payload)
    monkeypatch.setattr(tinfoil, "MANIFEST_PATH", manifest_path)

    result = tinfoil.fetch()
    notes = tinfoil.write_provider_manifest(result)
    written = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert notes == [
        "tinfoil: refreshed provider_models/tinfoil.json "
        "(1 priced rows, appended 1)"
    ]
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
            "input_token_price_per_m": 700_000,
            "output_token_price_per_m": 1_900_000,
            "cached_input_token_price_per_m": 125_000,
        }
    ]


def test_tinfoil_deepseek_route_is_confidential_and_uses_live_prices() -> None:
    endpoint = MODEL_ENDPOINTS["deepseek/deepseek-v4-flash@tinfoil/prepaid"]
    provider = PROVIDERS["tinfoil"]

    assert endpoint.upstream_id == "deepseek-v4-flash"
    assert endpoint.prompt_price_microdollars_per_million_tokens == 735_000
    assert endpoint.completion_price_microdollars_per_million_tokens == 1_995_000
    assert endpoint.price_tiers[0].prompt_cached_price_microdollars_per_million_tokens == 131_250
    assert provider.provider_zero_data_retention is True
    assert provider.provider_confidential_compute is True
    assert provider.provider_e2ee is True
