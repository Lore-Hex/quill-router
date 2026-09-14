"""Thinking Machines Lab Tinker model pricing refresh."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json
from scripts.pricing.parsers.thinkingmachines import _SAMPLER_IDS, _SERVERLESS_IDS

SLUG = "thinkingmachines"
URL = "https://tinker-docs.thinkingmachines.ai/tinker/models/"
SERVERLESS_URL = "https://tinker-docs.thinkingmachines.ai/tinker/serverless.json"
SAMPLER_URL = "https://tinker-docs.thinkingmachines.ai/tinker/models.json"
EXPECTED_MODELS = [
    "thinkingmachines/inkling",
    "thinkingmachines/inkling-small",
    "z-ai/glm-5.3",
]
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "thinkingmachines.json"
)


def _json_prices(
    payload: object, model_ids: dict[str, str], fields: tuple[str, str, str]
) -> dict[str, ModelPrice]:
    if not isinstance(payload, list):
        raise RuntimeError("thinkingmachines: pricing feed must be a list")
    prices: dict[str, ModelPrice] = {}
    for row in payload:
        if not isinstance(row, dict):
            raise RuntimeError("thinkingmachines: malformed pricing row")
        native_id = row.get("tinker_id")
        if not isinstance(native_id, str) or native_id not in model_ids:
            continue
        amounts = []
        for field in fields:
            value = row.get(field)
            if not isinstance(value, str) or re.fullmatch(r"\$\d+(?:\.\d{1,6})?", value) is None:
                raise RuntimeError(f"thinkingmachines: invalid {field} price for {native_id}")
            amounts.append(int(Decimal(value[1:]) * 1_000_000))
        prompt, cached, completion = amounts
        if prompt <= 0 or completion <= 0 or cached > prompt:
            raise RuntimeError(f"thinkingmachines: invalid token prices for {native_id}")
        model_id = model_ids[native_id]
        price = ModelPrice(prompt, completion, prompt_cached_micro_per_m=cached)
        if model_id in prices and prices[model_id] != price:
            raise RuntimeError(f"thinkingmachines: conflicting prices for {native_id}")
        prices[model_id] = price
    missing = set(model_ids.values()) - prices.keys()
    if missing:
        raise RuntimeError(f"thinkingmachines: missing exact model prices: {sorted(missing)}")
    return prices


def fetch() -> ProviderPricingResult:
    # These are the provider's documented stable interfaces. HTML columns can
    # move when presentation-only fields such as active parameters are added.
    prices = _json_prices(
        fetch_json(SERVERLESS_URL), _SERVERLESS_IDS, ("input", "cached_input", "output")
    )
    prices.update(
        _json_prices(fetch_json(SAMPLER_URL), _SAMPLER_IDS, ("prefill", "cached_prefill", "sample"))
    )
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=["verified exact deployed IDs against official serverless.json and models.json"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("thinkingmachines manifest has no models list")

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
        raise RuntimeError(f"thinkingmachines manifest did not update expected model(s): {missing}")

    raw["source"] = URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [
        "thinkingmachines: refreshed provider_models/thinkingmachines.json "
        f"({len(updated)} priced rows)"
    ]
