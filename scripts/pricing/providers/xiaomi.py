"""Xiaomi MiMo first-party pricing refresh."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "xiaomi"
URL = "https://r.jina.ai/https://mimo.mi.com/docs/en-US/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "xiaomi.json"
)

EXPECTED_MODELS = [
    "xiaomi/mimo-v2.5",
    "xiaomi/mimo-v2.5-pro",
    "xiaomi/mimo-v2.5-pro-ultraspeed",
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
        raise RuntimeError("xiaomi manifest has no models list")

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
        if tier.prompt_cached_micro_per_m is not None:
            row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
        else:
            row.pop("cached_input_token_price_per_m", None)
        updated.append(model_id)

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"xiaomi manifest did not update expected model(s): {missing}")

    raw["source"] = "https://mimo.mi.com/docs/en-US/pricing"
    raw["generated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [f"xiaomi: refreshed provider_models/xiaomi.json ({len(updated)} priced rows)"]
