from __future__ import annotations

import json
from pathlib import Path

from scripts.pricing import refresh
from scripts.pricing.base import ModelPrice, ProviderPricingResult
from scripts.pricing.parsers import makora as makora_parser
from scripts.pricing.providers import makora

MAKORA_LINEUP_HTML = """
The lineup
## The Latest Models. Competitive Pricing

GLM-5.2
z.AI
Input $1.35/M tokens
Output $3.99/M tokens
Cache Read $0.24/M tokens
[Try Now](https://app.makora.com/)

Kimi-K2.7-Code
moonshot ai
Input $0.76/M tokens
Output $3.7749/M tokens
Cache $0.5757/M tokens
[Try Now](https://app.makora.com/)

DeepSeek-V4-Pro
Input $1.3180/M tokens
Output $2.6361/M tokens
Cache $0.9885/M tokens
[Try Now](https://app.makora.com/)

Qwen3.6-27B-NVFP4
Input $0.4671/M tokens
Output $3.4592/M tokens
Cache $0.3503/M tokens
[Try Now](https://app.makora.com/)

Llama-3.3-70B-Instruct
Input $0.18/M tokens
Output $0.40/M tokens
Cache $0.15/M tokens
[Try Now](https://app.makora.com/)

DeepSeek V4 Flash
deepseek
Input $0.1134/M tokens
Output $0.2791/M tokens
Cache $0.0851/M tokens
[Try Now](https://app.makora.com/)

Qwen3.6-35B-A3B
alibaba
Input $0.1720/M tokens
Output $1.2002/M tokens
Cache $0.1290/M tokens
[Try Now](https://app.makora.com/)
"""


def test_makora_parser_extracts_public_lineup_prices() -> None:
    prices = makora_parser.parse(MAKORA_LINEUP_HTML)

    assert prices["deepseek/deepseek-v4-flash"] == {
        "prompt_micro_per_m": 113_400,
        "completion_micro_per_m": 279_100,
        "prompt_cached_micro_per_m": 85_100,
    }
    assert prices["deepseek/deepseek-v4-pro"] == {
        "prompt_micro_per_m": 1_318_000,
        "completion_micro_per_m": 2_636_100,
        "prompt_cached_micro_per_m": 988_500,
    }
    assert prices["z-ai/glm-5.2"] == {
        "prompt_micro_per_m": 1_350_000,
        "completion_micro_per_m": 3_990_000,
        "prompt_cached_micro_per_m": 240_000,
    }
    assert prices["z-ai/glm-5.2-nvfp4"] == prices["z-ai/glm-5.2"]
    assert prices["moonshotai/kimi-k2.7-code"] == {
        "prompt_micro_per_m": 760_000,
        "completion_micro_per_m": 3_774_900,
        "prompt_cached_micro_per_m": 575_700,
    }
    assert prices["meta-llama/llama-3.3-70b-instruct"] == {
        "prompt_micro_per_m": 180_000,
        "completion_micro_per_m": 400_000,
        "prompt_cached_micro_per_m": 150_000,
    }
    assert (
        prices["amd/llama-3.3-70b-instruct-fp8-kv"]
        == prices["meta-llama/llama-3.3-70b-instruct"]
    )
    assert prices["qwen/qwen3.6-35b-a3b"] == {
        "prompt_micro_per_m": 172_000,
        "completion_micro_per_m": 1_200_200,
        "prompt_cached_micro_per_m": 129_000,
    }


def test_makora_provider_updates_supplemental_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    manifest_path = tmp_path / "makora.json"
    manifest_path.write_text(
        json.dumps(
            {
                "provider": "makora",
                "source": "https://inference.makora.com/v1/models",
                "generated_at": "2026-01-01T00:00:00Z",
                "model_count": 3,
                "models": [
                    {
                        "id": "deepseek/deepseek-v4-flash",
                        "upstream_id": "deepseek-ai/DeepSeek-V4-Flash",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "endpoints": ["chat/completions"],
                    },
                    {
                        "id": "google/gemma-4-26b-a4b-it",
                        "upstream_id": "google/gemma-4-26B-A4B",
                        "input_token_price_per_m": 60_000,
                        "output_token_price_per_m": 330_000,
                        "endpoints": ["chat/completions"],
                    },
                    {
                        "id": "z-ai/glm-5.2",
                        "upstream_id": "zai-org/GLM-5.2-FP8",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "endpoints": ["chat/completions"],
                    },
                    {
                        "id": "z-ai/glm-5.2-nvfp4",
                        "upstream_id": "zai-org/GLM-5.2-NVFP4",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "endpoints": ["chat/completions"],
                    },
                    {
                        "id": "moonshotai/kimi-k2.7-code",
                        "upstream_id": "moonshotai/Kimi-K2.7-Code",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "endpoints": ["chat/completions"],
                    },
                    {
                        "id": "deepseek/deepseek-v4-pro",
                        "upstream_id": "deepseek-ai/DeepSeek-V4-Pro",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "endpoints": ["chat/completions"],
                    },
                    {
                        "id": "meta-llama/llama-3.3-70b-instruct",
                        "upstream_id": "meta-llama/Llama-3.3-70B-Instruct",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "endpoints": ["chat/completions"],
                    },
                    {
                        "id": "qwen/qwen3.6-27b",
                        "upstream_id": "unsloth/Qwen3.6-27B-NVFP4",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "endpoints": ["chat/completions"],
                    },
                    {
                        "id": "qwen/qwen3.6-35b-a3b",
                        "upstream_id": "unsloth/Qwen3.6-35B-A3B-NVFP4",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "endpoints": ["chat/completions"],
                    },
                    {
                        "id": "amd/llama-3.3-70b-instruct-fp8-kv",
                        "upstream_id": "amd/Llama-3.3-70B-Instruct-FP8-KV",
                        "input_token_price_per_m": 1,
                        "output_token_price_per_m": 1,
                        "endpoints": ["chat/completions"],
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(makora, "MANIFEST_PATH", manifest_path)

    parsed = makora_parser.parse(MAKORA_LINEUP_HTML)
    notes = makora.write_provider_manifest(
        ProviderPricingResult(
            slug="makora",
            prices={
                model_id: ModelPrice(
                    row["prompt_micro_per_m"],
                    row["completion_micro_per_m"],
                    prompt_cached_micro_per_m=row.get("prompt_cached_micro_per_m"),
                )
                for model_id, row in parsed.items()
            },
            source="deterministic",
            fetched_url=makora.URL,
        )
    )

    assert notes == ["makora: refreshed provider_models/makora.json (9 priced rows)"]
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    by_id = {row["id"]: row for row in raw["models"]}
    assert by_id["deepseek/deepseek-v4-flash"]["input_token_price_per_m"] == 113_400
    assert by_id["deepseek/deepseek-v4-flash"]["output_token_price_per_m"] == 279_100
    assert by_id["deepseek/deepseek-v4-flash"]["cached_input_token_price_per_m"] == 85_100
    assert by_id["z-ai/glm-5.2"]["input_token_price_per_m"] == 1_350_000
    assert by_id["google/gemma-4-26b-a4b-it"]["input_token_price_per_m"] == 60_000
    assert raw["generated_at"] != "2026-01-01T00:00:00Z"


def test_provider_model_manifests_have_hourly_refresh_path() -> None:
    legacy_manual = {
        # These existed before the hourly provider-adapter rule and should be
        # converted separately. New provider manifests must not expand this set.
        "minimax",
        "nebius",
        "xiaomi",
    }
    manifest_slugs = {
        path.stem
        for path in (Path("src/trusted_router/data/provider_models")).glob("*.json")
    }
    provider_modules = {
        path.stem for path in Path("scripts/pricing/providers").glob("*.py")
    } - {"__init__"}
    hourly = set(refresh.PROVIDER_SLUGS)

    missing_modules = sorted(manifest_slugs - provider_modules - legacy_manual)
    missing_hourly = sorted((manifest_slugs & provider_modules) - hourly)

    assert not missing_modules
    assert not missing_hourly
