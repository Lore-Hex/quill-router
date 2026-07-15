"""Makora — provider-native pricing refresh.

Makora's OpenAI-compatible `/v1/models` feed exposes model ids and context
windows but not prices. The public homepage "lineup" publishes per-token
prices for the headline models, so the hourly refresh parses that page and
updates `data/provider_models/makora.json`, which is what the runtime catalog
uses for Makora routes.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "makora"
URL = "https://www.makora.com/"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "makora.json"
)

EXPECTED_MODELS = [
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.2",
    "z-ai/glm-5.2-nvfp4",
    "moonshotai/kimi-k2.7-code",
    "meta-llama/llama-3.3-70b-instruct",
    "qwen/qwen3.6-27b",
    "qwen/qwen3.6-35b-a3b",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Update Makora's supplemental runtime manifest from parsed prices.

    The shared hourly snapshot merger updates `openrouter_snapshot.json`.
    Makora's actual runtime routes live in `provider_models/makora.json`, so
    this hook keeps that manifest from becoming a manually maintained price
    island.
    """

    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("makora manifest has no models list")

    updated: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str):
            continue
        price = result.prices.get(model_id)
        if price is None:
            # Gemma is intentionally still a fallback row because Makora's
            # public lineup does not publish a Gemma price today.
            continue
        tier = price.tiers[0]
        row["input_token_price_per_m"] = tier.prompt_micro_per_m
        row["output_token_price_per_m"] = tier.completion_micro_per_m
        if tier.prompt_cached_micro_per_m is not None:
            row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
        else:
            row.pop("cached_input_token_price_per_m", None)
        updated.append(model_id)

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"makora manifest did not update expected model(s): {missing}")
    if not updated:
        raise RuntimeError("makora manifest update touched no rows")

    raw["_about"] = (
        "Provider-native supplement for Makora Inference routes. Makora's "
        "/v1/models feed publishes native model ids and context windows but "
        "not pricing. Prices are refreshed hourly from Makora's public "
        "homepage lineup where listed; Gemma keeps the OpenRouter canonical "
        "price fallback because Makora does not currently publish a Gemma "
        "price on its homepage/pricing page/API feed."
    )
    raw["pricing_source"] = (
        "https://www.makora.com/; https://openrouter.ai/api/v1/models for Gemma fallback"
    )
    raw["generated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    raw["model_count"] = len(rows)

    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [f"makora: refreshed provider_models/makora.json ({len(updated)} priced rows)"]
