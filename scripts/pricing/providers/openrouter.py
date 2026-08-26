"""ByteDance Seed 2.1 Turbo pricing through OpenRouter's provider endpoint.

ByteDance does not sell a provider-direct Seed credential this deployment can
obtain, so OpenRouter is the actual downstream API and billing source for this
route and its endpoint feed is authoritative. Same shape as ``meta.py``:
OpenRouter is a transport for ONE explicitly allowlisted route, not a general
aggregator, and the price source is pinned to that route's endpoint document.

The route is chosen deliberately: the upstream ``Seed`` endpoint is ByteDance's
own, so this adds a vendor the catalogue cannot otherwise reach. Routes whose
only upstream is a provider we ALREADY hold directly (Meituan LongCat, served
by AtlasCloud) are not worth a second hop and are excluded.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json, validate

SLUG = "openrouter"
MODEL_ID = "bytedance-seed/seed-2-1-turbo"
URL = f"https://openrouter.ai/api/v1/models/{MODEL_ID}/endpoints"
EXPECTED_MODELS = [MODEL_ID]
PROVIDER_NAME = "Seed"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "openrouter.json"
)


def _microdollars_per_million(raw: Any) -> int:
    try:
        return int((Decimal(str(raw)) * Decimal(1_000_000_000_000)).to_integral_value())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(f"openrouter: invalid per-token price {raw!r}") from exc


def _seed_endpoint(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list):
        raise RuntimeError("openrouter: endpoint API returned an unexpected shape")
    for row in endpoints:
        if isinstance(row, dict) and row.get("provider_name") == PROVIDER_NAME:
            return row
    raise RuntimeError(f"openrouter: endpoint API did not contain the {PROVIDER_NAME} route")


def fetch() -> ProviderPricingResult:
    row = _seed_endpoint(fetch_json(URL))
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        raise RuntimeError("openrouter: Seed endpoint has no pricing object")
    cached = pricing.get("input_cache_read")
    price = ModelPrice(
        prompt_micro_per_m=_microdollars_per_million(pricing.get("prompt")),
        completion_micro_per_m=_microdollars_per_million(pricing.get("completion")),
        prompt_cached_micro_per_m=(
            _microdollars_per_million(cached) if cached not in (None, "") else None
        ),
    )
    prices = {MODEL_ID: price}
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError(f"openrouter: invalid Seed pricing: {errors}")
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
        raise RuntimeError("openrouter: provider manifest must contain exactly one model")
    price = result.prices.get(MODEL_ID)
    if price is None:
        raise RuntimeError("openrouter: refreshed pricing did not include Seed")
    row = rows[0]
    tier = price.tiers[0]
    row["input_token_price_per_m"] = tier.prompt_micro_per_m
    row["output_token_price_per_m"] = tier.completion_micro_per_m
    if tier.prompt_cached_micro_per_m is not None:
        row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
    else:
        row.pop("cached_input_token_price_per_m", None)
    raw["source"] = URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return ["openrouter: refreshed Seed 2.1 Turbo pricing from OpenRouter's Seed endpoint"]
