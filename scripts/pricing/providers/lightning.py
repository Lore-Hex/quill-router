"""Lightning AI — human-only provider config.

Lightning publishes per-model pricing in its `/v1/models` response
(under each entry's `pricing` block with `input_cost_per_token` +
`output_cost_per_token` as USD/token). API-direct path; no HTML
scraping, no LLM self-heal.

Auth: Bearer token in `LIGHTNING_API_KEY`. Without it, returns 401
and Lightning is one failure under MAX_TOLERATED_FAILURES — every
other provider still refreshes normally.

OR-canonical model id mapping is small today (just gemma-4 +
llama-3.3 to start). Extend `_NATIVE_TO_OR_ID` when we add more
Lightning-keyed models to the catalog.
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

SLUG = "lightning"
URL = "https://lightning.ai/api/v1/models"

EXPECTED_MODELS = [
    # gemma-4 is the headline model in this batch; if the parser
    # ever produces zero gemma-4 prices we want validation to flag it.
    "google/gemma-4-31b-it",
]


# Lightning native ids → OR-canonical. Lightning prefixes their hosted
# variants with `lightning-ai/` (e.g. `lightning-ai/gemma-4-31B-it`,
# `lightning-ai/llama-3.3-70b`). The OR-canonical form drops the prefix
# and lowercases the size token.
_NATIVE_TO_OR_ID = {
    "lightning-ai/gemma-4-31B-it": "google/gemma-4-31b-it",
    "lightning-ai/gemma-4-26B-A4B-it": "google/gemma-4-26b-a4b-it",
    "lightning-ai/llama-3.3-70b": "meta-llama/llama-3.3-70b-instruct",
    "lightning-ai/DeepSeek-V3.1": "deepseek/deepseek-v3.1",
}


def fetch() -> ProviderPricingResult:
    api_key = os.environ.get("LIGHTNING_API_KEY")
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
        pricing = row.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        # Lightning encodes prices as USD/token; convert to micro/M
        # (1 USD/token = 1e12 micro/M, so 1.4e-7 USD/token = 1.4e5
        # micro/M = $0.14/M).
        try:
            prompt_per_token = float(pricing.get("input_cost_per_token") or 0)
            completion_per_token = float(pricing.get("output_cost_per_token") or 0)
        except (TypeError, ValueError):
            continue
        if prompt_per_token <= 0 or completion_per_token <= 0:
            continue
        prompt_micro_per_m = int(round(prompt_per_token * 1_000_000_000_000))
        completion_micro_per_m = int(round(completion_per_token * 1_000_000_000_000))
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=prompt_micro_per_m,
            completion_micro_per_m=completion_micro_per_m,
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
