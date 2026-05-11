"""DeepInfra — human-only provider config.

DeepInfra hosts a large open-weight catalog (Llama, Gemma 4, Qwen,
DeepSeek, etc.). The OpenAI-compatible endpoint is at
`api.deepinfra.com/v1/openai`. Pricing is exposed per-model inside the
/v1/openai/models response under `metadata.pricing.input_tokens` and
`metadata.pricing.output_tokens` (USD per million tokens — DeepInfra
is the only provider in this batch that's already in
dollars-per-million rather than USD/token).

API-direct, no HTML scraping, no LLM self-heal. Auth: Bearer token in
`DEEPINFRA_API_KEY`. Without it the fetch 401s and DeepInfra counts as
one failure under MAX_TOLERATED_FAILURES.
"""
from __future__ import annotations

import os

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    validate,
)

SLUG = "deepinfra"
URL = "https://api.deepinfra.com/v1/openai/models"

EXPECTED_MODELS = [
    "google/gemma-4-31b-it",
]


# DeepInfra native ids → OR-canonical. DeepInfra mostly uses upstream
# author/model paths (e.g. `google/gemma-4-31B-it`) so this map is
# mostly normalize-the-case + drop-vendor-bumps. Extend as we add
# new DeepInfra-keyed models to the catalog.
_NATIVE_TO_OR_ID = {
    "google/gemma-4-31B-it": "google/gemma-4-31b-it",
    "google/gemma-4-26B-A4B-it": "google/gemma-4-26b-a4b-it",
    "google/gemma-3-27b-it": "google/gemma-3-27b-it",
    "google/gemma-3-12b-it": "google/gemma-3-12b-it",
    "google/gemma-3-4b-it": "google/gemma-3-4b-it",
    "meta-llama/Meta-Llama-3.1-70B-Instruct": "meta-llama/llama-3.1-70b-instruct",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "deepseek-ai/DeepSeek-V3.1": "deepseek/deepseek-v3.1",
    "Qwen/Qwen3.5-27B": "qwen/qwen3.5-27b",
}


def fetch() -> ProviderPricingResult:
    api_key = os.environ.get("DEEPINFRA_API_KEY")
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
    rows = payload.get("data") or []
    prices: dict[str, ModelPrice] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        or_id = _NATIVE_TO_OR_ID.get(native_id)
        if or_id is None:
            continue
        meta = row.get("metadata") or {}
        pricing = meta.get("pricing") if isinstance(meta, dict) else None
        if not isinstance(pricing, dict):
            continue
        # DeepInfra encodes prices as USD per million tokens directly
        # (e.g. 0.13 for $0.13/M). Convert to microdollars per million
        # by multiplying by 1_000_000.
        try:
            prompt = float(pricing.get("input_tokens") or 0)
            completion = float(pricing.get("output_tokens") or 0)
        except (TypeError, ValueError):
            continue
        if prompt <= 0 or completion <= 0:
            continue
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=int(round(prompt * 1_000_000)),
            completion_micro_per_m=int(round(completion * 1_000_000)),
        )

    notes: list[str] = []
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )
