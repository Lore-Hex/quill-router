"""xAI language-model discovery and pricing from its authenticated API."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import ModelPrice, PriceTier, ProviderPricingResult, fetch_json, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import positive_int, probe_openai_chat

SLUG = "grok"
BASE_URL = "https://api.x.ai/v1"
URL = f"{BASE_URL}/language-models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "grok.json"
)
EXPECTED_MODELS = ["x-ai/grok-4.6", "x-ai/grok-4.5"]

_NATIVE_TO_MODEL_ID = {
    "grok-4.20-multi-agent-0309": "x-ai/grok-4.20-multi-agent",
    "grok-4.20-0309-reasoning": "x-ai/grok-4.20-reasoning",
    "grok-4.20-0309-non-reasoning": "x-ai/grok-4.20",
    "grok-4-1-fast-reasoning": "x-ai/grok-4-1-fast-reasoning",
    "grok-4-1-fast-non-reasoning": "x-ai/grok-4-1-fast",
}
UPSTREAM_ID_MAP: dict[str, str] = {
    model_id: native_id for native_id, model_id in _NATIVE_TO_MODEL_ID.items()
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _model_id(native_id: str) -> str | None:
    value = native_id.strip().casefold()
    if not value.startswith("grok-"):
        return None
    return _NATIVE_TO_MODEL_ID.get(value) or f"x-ai/{value}"


def _microdollars_per_million(raw: object) -> int | None:
    value = positive_int(raw)
    return value * 100 if value is not None else None


def _price(row: dict[str, Any]) -> ModelPrice | None:
    prompt = _microdollars_per_million(row.get("prompt_text_token_price"))
    completion = _microdollars_per_million(row.get("completion_text_token_price"))
    if prompt is None or completion is None:
        return None
    cached = _microdollars_per_million(row.get("cached_prompt_text_token_price"))
    threshold = positive_int(row.get("long_context_threshold"))
    long_prompt = _microdollars_per_million(
        row.get("prompt_text_token_price_long_context")
    )
    long_completion = _microdollars_per_million(
        row.get("completion_text_token_price_long_context")
    )
    long_cached = _microdollars_per_million(
        row.get("cached_prompt_text_token_price_long_context")
    )
    if threshold is None or long_prompt is None or long_completion is None:
        return ModelPrice(
            prompt,
            completion,
            prompt_cached_micro_per_m=cached,
        )
    return ModelPrice(
        tiers=[
            PriceTier(threshold, prompt, completion, cached),
            PriceTier(None, long_prompt, long_completion, long_cached or cached),
        ]
    )


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    api_key = os.environ.get("GROK_API_KEY") or os.environ.get("XAI_API_KEY")
    if not api_key:
        raise RuntimeError("GROK_API_KEY is required for model discovery")
    payload = fetch_json(
        URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    raw_rows = payload.get("models") if isinstance(payload, dict) else None
    rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for source in rows:
        native_id = source.get("id")
        if not isinstance(native_id, str):
            continue
        output_modalities = {
            str(value).casefold() for value in (source.get("output_modalities") or [])
        }
        if output_modalities and "text" not in output_modalities:
            continue
        model_id = _model_id(native_id)
        price = _price(source)
        if model_id is None or price is None:
            continue
        UPSTREAM_ID_MAP[model_id] = native_id
        prices[model_id] = price
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": native_id,
            "endpoints": ["chat/completions"],
            "input_modalities": [
                str(value) for value in (source.get("input_modalities") or ["text"])
            ],
            "output_modalities": [
                str(value) for value in (source.get("output_modalities") or ["text"])
            ],
        }
        context_length = positive_int(source.get("context_length"))
        if context_length is not None:
            row["context_length"] = context_length
        created = positive_int(source.get("created"))
        if created is not None:
            row["created"] = created
        discovered[model_id] = row

    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    checked = models_requiring_canary(MANIFEST_PATH, discovered)
    healthy = {
        model_id
        for model_id in checked
        if probe_openai_chat(
            base_url=BASE_URL,
            api_key=api_key,
            model=UPSTREAM_ID_MAP[model_id],
        )
    }
    apply_canary_results(
        discovered,
        checked_model_ids=checked,
        healthy_model_ids=healthy,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[
            f"discovered {len(discovered)} priced language models",
            f"canaried {len(checked)} new/held routes ({len(healthy)} healthy)",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
