"""Vercel AI Gateway: TypeSafe AI's Jev decision model, input-only pricing.

Vercel publishes per-token rates in its public, unauthenticated model list,
so this is a deterministic API read rather than a pricing-page parse. Only the
decision (``evaluation``) model TrustedRouter actually resells is read; Vercel's
chat catalog is deliberately ignored.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx

from scripts.pricing.base import ModelPrice, ProviderPricingResult

SLUG = "vercel-ai-gateway"
URL = "https://ai-gateway.vercel.sh/v1/models"
EXPECTED_MODELS = ["typesafe-ai/jev"]
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/vercel-ai-gateway.json"
)
# Fixed-shape input-only rows are not comparable with chat token prices.
INCLUDE_IN_PRICE_INDEX = False
_UNDERSTOOD_PRICING_KEYS = frozenset({"input", "output"})


def _micro_per_million(per_token: object) -> int:
    try:
        value = Decimal(str(per_token)) * Decimal(1_000_000) * Decimal(1_000_000)
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"{SLUG}: unparseable per-token price {per_token!r}") from exc
    if value != value.to_integral_value() or value < 0:
        raise RuntimeError(f"{SLUG}: price {per_token!r} is not a whole microdollar/M rate")
    return int(value)


def parse(payload: object) -> dict[str, ModelPrice]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"{SLUG}: model list has no data array")
    prices: dict[str, ModelPrice] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in EXPECTED_MODELS:
            continue
        if row.get("type") != "evaluation":
            raise RuntimeError(f"{SLUG}: {row.get('id')} is no longer an evaluation model")
        pricing = row.get("pricing")
        if not isinstance(pricing, dict):
            raise RuntimeError(f"{SLUG}: {row['id']} has no pricing object")
        # Vercel prices other models with input_tiers, regional, peak_pricing,
        # service_tiers and twenty more keys. Any of them on THIS row changes
        # what a request costs us while `input` alone still parses, so a flat
        # rate read from it would be published and billed wrong. Two keys are
        # understood; anything else stops the refresh.
        unknown = sorted(set(pricing) - _UNDERSTOOD_PRICING_KEYS)
        if unknown:
            raise RuntimeError(
                f"{SLUG}: {row['id']} pricing has keys this parser does not "
                f"understand {unknown}; a flat input rate would be wrong"
            )
        prompt = _micro_per_million(pricing.get("input"))
        completion = _micro_per_million(pricing.get("output", "0"))
        if prompt <= 0 or completion != 0:
            # The route bills input only. A provider that starts metering
            # output must fail the refresh loudly, not be billed at zero.
            raise RuntimeError(
                f"{SLUG}: {row['id']} pricing changed shape "
                f"(input={prompt}, output={completion}); decision routes are input-only"
            )
        prices[row["id"]] = ModelPrice(prompt, 0)
    missing = sorted(set(EXPECTED_MODELS) - set(prices))
    if missing:
        raise RuntimeError(f"{SLUG}: model list is missing {missing}")
    return prices


def fetch() -> ProviderPricingResult:
    response = httpx.get(URL, timeout=30, headers={"Accept": "application/json"})
    response.raise_for_status()
    return ProviderPricingResult(
        slug=SLUG,
        prices=parse(response.json()),
        source="api",
        fetched_url=URL,
        include_in_price_index=INCLUDE_IN_PRICE_INDEX,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    updated: list[str] = []
    for row in raw["models"]:
        price = result.prices.get(row["id"])
        if price is None:
            continue
        row["input_token_price_per_m"] = price.prompt_micro_per_m
        row["output_token_price_per_m"] = 0
        row["pricing_source"] = result.fetched_url
        updated.append(row["id"])
    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"{SLUG} manifest did not update required model(s): {missing}")
    raw["generated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    MANIFEST_PATH.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    return [
        f"{SLUG}: refreshed provider_models/{MANIFEST_PATH.name} ({len(updated)} priced rows)"
    ]
