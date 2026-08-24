"""Telnyx provider-native catalog and pricing refresh.

Telnyx's authenticated OpenAI-compatible model feed is authoritative for
models callable by the operator account. Pricing is reconciled from three
provider-owned sources:

1. positive rates in the authenticated model feed;
2. the current inference pricing page;
3. the public x402 model catalog for the remaining models.

The authenticated feed currently emits zero placeholders for most models.
Those values are never interpreted as free.
"""

from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_json,
    fetch_provider,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id
from scripts.pricing.openai_catalog import positive_int

SLUG = "telnyx"
BASE_URL = "https://api.telnyx.com/v2/ai/openai"
MODELS_URL = f"{BASE_URL}/models"
PRICING_URL = "https://telnyx.com/pricing/inference-api"
X402_MODELS_URL = "https://x402.telnyx.com/v1/models"
URL = PRICING_URL
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "telnyx.json"
)

_NATIVE_TO_OR_ID = {
    "google/gemma-2b-it": "google/gemma-2b-it",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "meta-llama/Meta-Llama-3.1-70B-Instruct": "meta-llama/llama-3.1-70b-instruct",
    "meta-llama/Meta-Llama-3.1-8B-Instruct": "meta-llama/llama-3.1-8b-instruct",
    "MiniMaxAI/MiniMax-M2.7": "minimax/minimax-m2.7",
    "MiniMaxAI/MiniMax-M3-MXFP8": "minimax/minimax-m3",
    "moonshotai/Kimi-K2.5": "moonshotai/kimi-k2.5",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "moonshotai/Kimi-K3": "moonshotai/kimi-k3",
    "Qwen/Qwen3-235B-A22B": "qwen/qwen3-235b-a22b",
    "zai-org/GLM-5.1-FP8": "z-ai/glm-5.1",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
}

EXPECTED_MODELS = list(_NATIVE_TO_OR_ID.values())
UPSTREAM_ID_MAP = {model_id: native_id for native_id, model_id in _NATIVE_TO_OR_ID.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _dollars_per_m_to_micro_per_m(value: object) -> int | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return int((parsed * Decimal(1_000_000)).to_integral_value(ROUND_HALF_UP))


def _model_price(
    *,
    prompt: object,
    completion: object,
    cached: object = None,
) -> ModelPrice | None:
    prompt_micro = _dollars_per_m_to_micro_per_m(prompt)
    completion_micro = _dollars_per_m_to_micro_per_m(completion)
    if prompt_micro is None or completion_micro is None:
        return None
    return ModelPrice(
        prompt_micro_per_m=prompt_micro,
        completion_micro_per_m=completion_micro,
        prompt_cached_micro_per_m=_dollars_per_m_to_micro_per_m(cached),
    )


def _live_catalog(
    payload: object,
) -> tuple[dict[str, dict[str, Any]], dict[str, ModelPrice]]:
    source_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        raise RuntimeError("telnyx: authenticated /models response has no data list")

    discovered: dict[str, dict[str, Any]] = {}
    direct_prices: dict[str, ModelPrice] = {}
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        if str(source.get("owned_by") or "").casefold() != "telnyx":
            continue
        if str(source.get("task") or "").casefold() != "text-generation":
            continue
        native_id = source.get("id")
        if not isinstance(native_id, str) or not native_id:
            continue
        model_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        if model_id is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        input_modalities = ["text"]
        if source.get("is_vision_supported") is True:
            input_modalities.append("image")
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": str(source.get("name") or native_id.split("/", 1)[-1]),
            "title": native_id,
            "model_type": "chat",
            "input_modalities": input_modalities,
            "output_modalities": ["text"],
            "endpoints": ["chat/completions"],
            "status": 1,
        }
        context_length = positive_int(source.get("context_length"))
        if context_length is not None:
            row["context_length"] = context_length
        max_output_tokens = positive_int(source.get("max_completion_tokens"))
        if max_output_tokens is not None:
            row["max_output_tokens"] = max_output_tokens
        regions = source.get("regions")
        if isinstance(regions, list):
            row["provider_regions"] = [
                str(region) for region in regions if isinstance(region, str) and region
            ]
        discovered[model_id] = row

        pricing = source.get("pricing")
        if not isinstance(pricing, dict):
            continue
        if str(pricing.get("unit") or "").casefold() != "1m_tokens":
            continue
        price = _model_price(
            prompt=pricing.get("input"),
            completion=pricing.get("output"),
            cached=pricing.get("cached_prompt"),
        )
        if price is not None:
            direct_prices[model_id] = price
    if not discovered:
        raise RuntimeError("telnyx: authenticated catalog returned no Telnyx text models")
    return discovered, direct_prices


def _x402_prices(payload: object) -> dict[str, ModelPrice]:
    source_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        raise RuntimeError("telnyx: x402 model catalog has no data list")
    prices: dict[str, ModelPrice] = {}
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        if str(source.get("owned_by") or "").casefold() != "telnyx":
            continue
        native_id = source.get("id")
        if not isinstance(native_id, str):
            continue
        model_id = _NATIVE_TO_OR_ID.get(native_id)
        if model_id is None:
            continue
        pricing = source.get("pricing")
        rates = pricing.get("rates") if isinstance(pricing, dict) else None
        if not isinstance(rates, dict):
            continue
        price = _model_price(
            prompt=rates.get("input"),
            completion=rates.get("output"),
            cached=rates.get("cached"),
        )
        if price is not None:
            prices[model_id] = price
    return prices


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("TELNYX_API_KEY")
    if not api_key:
        raise RuntimeError("telnyx: TELNYX_API_KEY is required")
    headers = {"Authorization": f"Bearer {api_key}"}
    live_payload = fetch_json(MODELS_URL, extra_headers=headers)
    discovered, direct_prices = _live_catalog(live_payload)
    x402_prices = _x402_prices(fetch_json(X402_MODELS_URL))
    page_result = fetch_provider(
        slug=SLUG,
        url=PRICING_URL,
        expected_models=[
            "moonshotai/kimi-k2.6",
            "z-ai/glm-5.2",
            "minimax/minimax-m3",
        ],
    )

    prices = {
        model_id: price
        for model_id, price in x402_prices.items()
        if model_id in discovered
    }
    prices.update(
        {
            model_id: price
            for model_id, price in page_result.prices.items()
            if model_id in discovered
        }
    )
    prices.update(direct_prices)
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))

    _DISCOVERED_MANIFEST_ROWS = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=MODELS_URL,
        notes=[
            f"discovered {len(discovered)} Telnyx-owned text models",
            "pricing precedence: authenticated catalog > current pricing page > x402 catalog",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=MODELS_URL,
    )
