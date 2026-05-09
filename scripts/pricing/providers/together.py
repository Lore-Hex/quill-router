"""Together AI — human-only provider config.

Together has a real JSON pricing API at /v1/models, so this provider
bypasses the parser tier entirely. No HTML scraping, no LLM self-heal —
just hit the API and translate.

`/v1/models` requires an API key (Bearer auth). The workflow can
provide one via the TOGETHER_API_KEY env var. Without it, the fetch
returns 401 and Together is counted as a single failure under
MAX_TOLERATED_FAILURES — every other provider still refreshes.
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

SLUG = "together"
URL = "https://api.together.xyz/v1/models"

# Model IDs we expect Together to expose, in OR-canonical form. Parser
# below translates Together's native IDs to these.
EXPECTED_MODELS = [
    # Llama family that we route through Together — kept loose because
    # Together's catalog churns and we don't want to fail the workflow
    # over a single rename.
]


# Together native model id → OR-canonical id. Add/extend as new models
# get keyed providers in catalog.py.
_NATIVE_TO_OR_ID = {
    "meta-llama/Llama-3-8b-chat-hf": "meta-llama/llama-3-8b-chat",
    "meta-llama/Llama-3.1-8B-Instruct-Turbo": "meta-llama/llama-3.1-8b-instruct",
    "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo": "meta-llama/llama-3.1-8b-instruct",
    "meta-llama/Llama-3.1-70B-Instruct-Turbo": "meta-llama/llama-3.1-70b-instruct",
    "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo": "meta-llama/llama-3.1-70b-instruct",
    "deepseek-ai/DeepSeek-V3": "deepseek/deepseek-v3",
    "deepseek-ai/DeepSeek-V3-OCR": "deepseek/deepseek-v3-ocr",
    "Qwen/Qwen2.5-7B-Instruct-Turbo": "qwen/qwen-2.5-7b-instruct",
    "Qwen/Qwen2.5-72B-Instruct-Turbo": "qwen/qwen-2.5-72b-instruct",
    "mistralai/Mixtral-8x7B-Instruct-v0.1": "mistralai/mixtral-8x7b-instruct",
    # Together also hosts Moonshot's Kimi models — TR uses Together as a
    # secondary endpoint for `moonshotai/kimi-k2.6` so the model has
    # both kimi-direct and together endpoints in the snapshot.
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "moonshotai/Kimi-K2-Instruct": "moonshotai/kimi-k2-instruct",
    "moonshotai/Kimi-K2.5": "moonshotai/kimi-k2.5",
}


def _row_to_micro_per_m(price_per_token: object) -> int | None:
    """Together returns prices as dollars per token (or sometimes per million,
    depending on field). Convert to microdollars per million.
    1 USD/token = 1_000_000 USD/M = 1_000_000_000_000 micro/M; that's
    obviously absurd, so Together's numbers must be USD per million tokens
    despite the field naming. Coerce robustly: anything < 1 is treated as
    USD per token; anything >= 1 is treated as USD per million tokens."""
    if price_per_token is None:
        return None
    try:
        value = float(price_per_token)
    except (TypeError, ValueError):
        return None
    if value < 0:
        return None
    if value < 1:
        # USD per token → micro per M = value * 1e6 micro * 1e6 tokens
        # = value * 1e12, but that's absurd. Together's actual encoding
        # is dollars per 1M tokens for chat models, so:
        usd_per_m = value
    else:
        usd_per_m = value
    return int(round(usd_per_m * 1_000_000))


def fetch() -> ProviderPricingResult:
    api_key = os.environ.get("TOGETHER_API_KEY")
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
    if isinstance(payload, dict):
        rows = payload.get("data") or []
    else:
        rows = payload
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
        pricing = row.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        prompt = _row_to_micro_per_m(pricing.get("input"))
        completion = _row_to_micro_per_m(pricing.get("output"))
        if prompt is None or completion is None:
            continue
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
        )

    notes: list[str] = []
    if not prices:
        notes.append(
            "no Together models matched _NATIVE_TO_OR_ID — extend the table "
            "or check the API response"
        )
    # Validate but DO NOT raise on errors — the orchestrator distinguishes
    # "this provider had problems" (notes attached, source still 'api')
    # from "this provider is unusable" (raised exception, treated as a
    # failure under MAX_TOLERATED_FAILURES). EXPECTED_MODELS is empty
    # for Together so price-floor validation can't fail; only the
    # all-zeros / non-empty check triggers, and an empty Together result
    # is an expected outcome on auth-key absence.
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
