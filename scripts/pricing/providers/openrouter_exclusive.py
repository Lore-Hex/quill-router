"""Price the one explicitly allowlisted OpenRouter-exclusive model.

This is not a generic OpenRouter provider adapter. Both discovery and the
attested gateway independently pin the route to ``stealth/ox-alpha``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json, validate

SLUG = "openrouter-exclusive"
MODEL_ID = "stealth/ox-alpha"
PROVIDER_NAME = "Stealth"
URL = f"https://openrouter.ai/api/v1/models/{MODEL_ID}/endpoints"
EXPECTED_MODELS = [MODEL_ID]
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "openrouter-exclusive.json"
)


def _microdollars_per_million(raw: Any) -> int:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(f"openrouter-exclusive: invalid per-token price {raw!r}") from exc
    if not value.is_finite() or value < 0:
        raise RuntimeError(f"openrouter-exclusive: invalid per-token price {raw!r}")
    return int((value * Decimal(1_000_000_000_000)).to_integral_value())


def _exclusive_endpoint(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        raise RuntimeError("openrouter-exclusive: endpoint API returned an unexpected shape")
    matches = [
        row
        for row in endpoints
        if isinstance(row, dict)
        and row.get("provider_name") == PROVIDER_NAME
        and row.get("model_id") == MODEL_ID
        and row.get("status") == 0
    ]
    if len(matches) != 1:
        raise RuntimeError("openrouter-exclusive: expected one healthy Stealth Ox Alpha endpoint")
    return matches[0]


def fetch() -> ProviderPricingResult:
    row = _exclusive_endpoint(fetch_json(URL))
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        raise RuntimeError("openrouter-exclusive: Ox Alpha endpoint has no pricing object")
    price = ModelPrice(
        prompt_micro_per_m=_microdollars_per_million(pricing.get("prompt")),
        completion_micro_per_m=_microdollars_per_million(pricing.get("completion")),
    )
    prices = {MODEL_ID: price}
    # OpenRouter explicitly publishes this preview at $0/$0. Keep the global
    # zero-price guard strict and opt out only for this exact endpoint adapter.
    errors = validate(prices, EXPECTED_MODELS, allow_all_zero=True)
    if errors:
        raise RuntimeError(f"openrouter-exclusive: invalid Ox Alpha pricing: {errors}")
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError("openrouter-exclusive: provider manifest must contain exactly one model")
    if rows[0].get("id") != MODEL_ID or rows[0].get("upstream_id") != MODEL_ID:
        raise RuntimeError("openrouter-exclusive: manifest model is not pinned to Ox Alpha")
    price = result.prices.get(MODEL_ID)
    if price is None:
        raise RuntimeError("openrouter-exclusive: refresh did not include Ox Alpha")
    tier = price.tiers[0]
    rows[0]["input_token_price_per_m"] = tier.prompt_micro_per_m
    rows[0]["output_token_price_per_m"] = tier.completion_micro_per_m
    raw["source"] = URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return ["openrouter-exclusive: refreshed Ox Alpha pricing from its exact endpoint"]
