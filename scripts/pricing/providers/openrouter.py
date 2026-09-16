"""Price explicitly approved OpenRouter-only routes from their endpoint feeds.

This is not general aggregator discovery: models and downstream operators must
both be allowlisted. Availability follows the live catalog, using the shared
manifest writer's delisting safeguards. Missing prices never mean free.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json, validate
from scripts.pricing.manifest import write_discovered_chat_manifest

SLUG = "openrouter"
URL = "https://openrouter.ai/api/v1/models"
MODEL_PROVIDERS = {
    "bytedance-seed/seed-2-1-turbo": "Seed",
    "stealth/union-alpha": "Stealth",
}
EXPECTED_MODELS = list(MODEL_PROVIDERS)
_FREE_MODELS = frozenset({"stealth/union-alpha"})
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
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
        value = Decimal(str(raw))
        if not value.is_finite() or value < 0:
            raise ValueError("price must be finite and non-negative")
        return int((value * Decimal(1_000_000_000_000)).to_integral_value())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError(f"openrouter: invalid per-token price {raw!r}") from exc


def _endpoint(payload: Any, model_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else None
    endpoints = data.get("endpoints") if isinstance(data, dict) else None
    if not isinstance(endpoints, list) or data.get("id") != model_id:
        raise RuntimeError("openrouter: endpoint API returned an unexpected shape")
    matches = [row for row in endpoints if isinstance(row, dict)
               and row.get("provider_name") == MODEL_PROVIDERS[model_id]
               and row.get("model_id") == model_id and row.get("status") == 0]
    if len(matches) != 1:
        raise RuntimeError(f"openrouter: expected one active approved endpoint for {model_id}")
    return data, matches[0]


def fetch() -> ProviderPricingResult:
    payload = fetch_json(URL)
    models = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(models, list) or not models:
        raise RuntimeError("openrouter: catalog API returned an empty or invalid model list")
    available = {row["id"] for row in models if isinstance(row, dict)
                 and isinstance(row.get("id"), str)}
    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for model_id in MODEL_PROVIDERS:
        if model_id not in available:
            continue
        source = f"{URL}/{model_id}/endpoints"
        data, row = _endpoint(fetch_json(source), model_id)
        pricing = row.get("pricing")
        if not isinstance(pricing, dict):
            raise RuntimeError(f"openrouter: {model_id} endpoint has no pricing object")
        cached = pricing.get("input_cache_read")
        price = ModelPrice(
            prompt_micro_per_m=_microdollars_per_million(pricing.get("prompt")),
            completion_micro_per_m=_microdollars_per_million(pricing.get("completion")),
            prompt_cached_micro_per_m=(
                _microdollars_per_million(cached) if cached not in (None, "") else None
            ),
        )
        errors = validate({model_id: price}, [], allow_all_zero=model_id in _FREE_MODELS)
        if errors:
            raise RuntimeError(f"openrouter: invalid {model_id} pricing: {errors}")
        context = row.get("context_length")
        if isinstance(context, bool) or not isinstance(context, int) or context <= 0:
            raise RuntimeError(f"openrouter: {model_id} has no valid context limit")
        architecture = data.get("architecture")
        if not isinstance(architecture, dict):
            raise RuntimeError(f"openrouter: {model_id} has no architecture")
        prices[model_id] = price
        discovered[model_id] = {
            "id": model_id, "upstream_id": model_id,
            "display_name": data.get("name") or model_id,
            "context_length": context,
            "max_completion_tokens": row.get("max_completion_tokens"),
            "input_modalities": architecture.get("input_modalities", ["text"]),
            "output_modalities": architecture.get("output_modalities", ["text"]),
            "supported_parameters": row.get("supported_parameters", []),
            "endpoints": ["chat/completions"],
            "pricing_source": source,
        }
    errors = validate(prices, EXPECTED_MODELS, allow_all_zero=set(prices) <= _FREE_MODELS)
    if errors:
        raise RuntimeError(f"openrouter: invalid pricing: {errors}")
    global _DISCOVERED_MANIFEST_ROWS
    _DISCOVERED_MANIFEST_ROWS = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result, manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL, pricing_source_url=URL,
    )
