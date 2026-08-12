"""xAI Grok — human-only provider config.

docs.x.ai's pricing tables moved from /docs/models to /developers/pricing
in 2026-05. The current page renders one row per model with $-priced
input/cache/output cells. The model name cell is often a markdown link
([grok-4.5](url)) rather than a bare slug. The parser handles both
(see parsers/grok.py).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "grok"
URL = "https://docs.x.ai/developers/pricing.md"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "grok.json"
)

EXPECTED_MODELS = [
    "x-ai/grok-4.6",
    "x-ai/grok-4.5",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Refresh every checked-in xAI route from the official tiered price table."""
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("grok manifest has no models list")

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

        first = price.tiers[0]
        row["input_token_price_per_m"] = first.prompt_micro_per_m
        row["output_token_price_per_m"] = first.completion_micro_per_m
        if first.prompt_cached_micro_per_m is None:
            row.pop("cached_input_token_price_per_m", None)
        else:
            row["cached_input_token_price_per_m"] = first.prompt_cached_micro_per_m

        if len(price.tiers) == 1:
            row.pop("price_tiers", None)
        else:
            row["price_tiers"] = [
                {
                    "max_prompt_tokens": tier.max_prompt_tokens,
                    "input_token_price_per_m": tier.prompt_micro_per_m,
                    "output_token_price_per_m": tier.completion_micro_per_m,
                    **(
                        {
                            "cached_input_token_price_per_m": (
                                tier.prompt_cached_micro_per_m
                            )
                        }
                        if tier.prompt_cached_micro_per_m is not None
                        else {}
                    ),
                }
                for tier in price.tiers
            ]
        updated.append(model_id)

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"grok manifest did not update expected model(s): {missing}")

    raw["source"] = result.fetched_url or URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [f"grok: refreshed provider_models/grok.json ({len(updated)} priced rows)"]
