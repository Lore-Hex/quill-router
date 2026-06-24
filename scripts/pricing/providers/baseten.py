"""Baseten — provider-native model catalog and pricing.

Baseten publishes an OpenAI-compatible inference API at
https://inference.baseten.co/v1. Its `/models` response includes exact native
model ids, context lengths, and per-token prices as decimal strings. This
module converts those rates into TrustedRouter's integer microdollars per
million tokens and keeps `UPSTREAM_ID_MAP` in sync so the enclave calls the
provider-native id, not a guessed lowercase slug.
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

SLUG = "baseten"
URL = "https://inference.baseten.co/v1/models"

EXPECTED_MODELS = [
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.7-code",
]

_NATIVE_TO_OR_ID = {
    "openai/gpt-oss-120b": "openai/gpt-oss-120b",
    "zai-org/GLM-4.7": "z-ai/glm-4.7",
    "moonshotai/Kimi-K2.5": "moonshotai/kimi-k2.5",
    "zai-org/GLM-5": "z-ai/glm-5",
    "nvidia/Nemotron-120B-A12B": "nvidia/nemotron-120b-a12b",
    "zai-org/GLM-5.1": "z-ai/glm-5.1",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B": ("nvidia/nvidia-nemotron-3-ultra-550b-a55b"),
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
}

UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}


def _price_to_micro_per_m(value: object) -> int | None:
    """Baseten returns dollars/token; TR stores microdollars/million tokens."""

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return int((parsed * Decimal("1000000000000")).to_integral_value(ROUND_HALF_UP))


def fetch() -> ProviderPricingResult:
    api_key = os.environ.get("BASETEN_API_KEY")
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
        pricing = row.get("pricing")
        if not isinstance(pricing, dict):
            continue
        prompt = _price_to_micro_per_m(pricing.get("prompt") or pricing.get("input"))
        completion = _price_to_micro_per_m(pricing.get("completion") or pricing.get("output"))
        if prompt is None or completion is None:
            continue
        cache_read = _price_to_micro_per_m(
            pricing.get("input_cache_read") or pricing.get("cache_read")
        )
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
