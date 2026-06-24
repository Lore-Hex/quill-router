"""Wafer — provider-native model catalog and pricing.

Wafer exposes an OpenAI-compatible API at https://pass.wafer.ai/v1. Its
`/models` response is the source of truth for model availability, ZDR support,
capabilities, and prices. Prices are published as cents per million tokens,
so this module converts directly to integer microdollars per million tokens.
"""

from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id

SLUG = "wafer"
URL = "https://pass.wafer.ai/v1/models"

EXPECTED_MODELS = [
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
    "minimax/minimax-m3",
]

_NATIVE_TO_OR_ID = {
    "GLM-5.1": "z-ai/glm-5.1",
    "GLM-5.2": "z-ai/glm-5.2",
    "Kimi-K2.6": "moonshotai/kimi-k2.6",
    "Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
    "Qwen3.5-397B-A17B": "qwen/qwen3.5-397b-a17b",
    "Qwen3.6-35B-A3B": "qwen/qwen3.6-35b-a3b",
    "qwen3.6-max-preview": "qwen/qwen3.6-max-preview",
    "qwen3.7-max": "qwen/qwen3.7-max",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "MiniMax-M3": "minimax/minimax-m3",
}

UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}


def _cents_to_micro_per_m(value: object) -> int | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return int((parsed * Decimal("10000")).to_integral_value(ROUND_HALF_UP))


def fetch() -> ProviderPricingResult:
    api_key = os.environ.get("WAFER_API_KEY")
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(URL, headers=headers)
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = []
    prices: dict[str, ModelPrice] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        or_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        if or_id is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, or_id, native_id)
        wafer = row.get("wafer")
        pricing = wafer.get("pricing") if isinstance(wafer, dict) else None
        if not isinstance(pricing, dict):
            continue
        prompt = _cents_to_micro_per_m(pricing.get("input_cents_per_million"))
        completion = _cents_to_micro_per_m(pricing.get("output_cents_per_million"))
        if prompt is None or completion is None:
            continue
        cache_read = _cents_to_micro_per_m(pricing.get("cache_read_cents_per_million"))
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cache_read,
        )

    notes: list[str] = []
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")
        raise RuntimeError("; ".join(errors))

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )
