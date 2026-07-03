"""MiniMax first-party API pricing refresh."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "minimax"
URL = "https://r.jina.ai/https://platform.minimax.io/subscribe/token-plan?tab=api-enterprise"
JINA_HEADERS = {"X-Return-Format": "markdown"}
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "minimax.json"
)

EXPECTED_MODELS = [
    "minimax/minimax-m3",
    "minimax/minimax-m2.7",
    "minimax/minimax-m2.7-highspeed",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("minimax manifest has no models list")

    updated: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str):
            continue
        price = result.prices.get(model_id)
        if price is None:
            continue
        tier = price.tiers[0]
        row["input_token_price_per_m"] = tier.prompt_micro_per_m
        row["output_token_price_per_m"] = tier.completion_micro_per_m
        if len(price.tiers) > 1:
            row["price_tiers"] = [
                {
                    "max_prompt_tokens": price_tier.max_prompt_tokens,
                    "input_token_price_per_m": price_tier.prompt_micro_per_m,
                    "output_token_price_per_m": price_tier.completion_micro_per_m,
                    "cached_input_token_price_per_m": price_tier.prompt_cached_micro_per_m,
                }
                for price_tier in price.tiers
            ]
        elif tier.prompt_cached_micro_per_m is not None:
            row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
            row.pop("price_tiers", None)
        else:
            row.pop("cached_input_token_price_per_m", None)
            row.pop("price_tiers", None)
        updated.append(model_id)

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"minimax manifest did not update expected model(s): {missing}")

    raw["source"] = "https://platform.minimax.io/subscribe/token-plan?tab=api-enterprise"
    raw["generated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [f"minimax: refreshed provider_models/minimax.json ({len(updated)} priced rows)"]
