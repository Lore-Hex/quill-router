"""GMI Cloud — human-only provider config.

GMI Cloud publishes per-model pricing in its `/v1/models` response.
Each model has a `pricing` block; for chat models it's an object
with `prompt` + `completion` keys whose values are USD/token strings:

    "pricing": {
        "prompt":     "0.000000130",
        "completion": "0.000000400",
        ...
    }

Tiered models return `pricing` as a list of `{tier, min_context, prompt, completion, ...}`
objects; we only consume the lowest tier (tier 0 or the entry with
`min_context=0`) for the headline rate.

API-direct, no HTML scraping, no LLM self-heal. Auth: Bearer token
in `GMI_API_KEY` env. Without it, the fetch 401s and GMI counts as
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

SLUG = "gmi"
URL = "https://api.gmi-serving.com/v1/models"

EXPECTED_MODELS = [
    "google/gemma-4-31b-it",
]


# GMI native ids → OR-canonical. GMI mostly serves under standard
# author/model paths already (e.g. `google/gemma-4-31b-it`,
# `deepseek-ai/DeepSeek-V4-Pro`), so this map is mostly identity
# transforms with a few normalizations.
_NATIVE_TO_OR_ID = {
    "google/gemma-4-31b-it": "google/gemma-4-31b-it",
    "google/gemma-4-26b-a4b-it": "google/gemma-4-26b-a4b-it",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    "deepseek-ai/DeepSeek-V3.1": "deepseek/deepseek-v3.1",
    "zai-org/GLM-5-FP8": "z-ai/glm-5",
    "zai-org/GLM-5.1-FP8": "z-ai/glm-5.1",
    "anthropic/claude-opus-4.7": "anthropic/claude-opus-4.7",
    "openai/gpt-5.4-nano": "openai/gpt-5.4-nano",
    "openai/gpt-5.5": "openai/gpt-5.5",
}


def _extract_lowest_tier(pricing: object) -> tuple[float, float] | None:
    """GMI returns `pricing` as either a dict (flat) or a list of
    tier rows. Return (prompt_per_token, completion_per_token) for
    the lowest tier (or the flat case), in USD/token. None on any
    structural mismatch or zero/negative rates."""
    if isinstance(pricing, dict):
        prompt = pricing.get("prompt")
        completion = pricing.get("completion")
    elif isinstance(pricing, list):
        # Pick the entry with min_context = 0 (the cheapest, smallest-
        # context tier) — that's the headline rate to display.
        candidate = None
        for row in pricing:
            if not isinstance(row, dict):
                continue
            if (row.get("min_context") or 0) == 0:
                candidate = row
                break
        if candidate is None and pricing:
            candidate = pricing[0] if isinstance(pricing[0], dict) else None
        if candidate is None:
            return None
        prompt = candidate.get("prompt")
        completion = candidate.get("completion")
    else:
        return None
    try:
        p = float(prompt or 0)
        c = float(completion or 0)
    except (TypeError, ValueError):
        return None
    if p <= 0 or c <= 0:
        return None
    return p, c


def fetch() -> ProviderPricingResult:
    api_key = os.environ.get("GMI_API_KEY")
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
        rates = _extract_lowest_tier(row.get("pricing"))
        if rates is None:
            continue
        prompt_per_token, completion_per_token = rates
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=int(round(prompt_per_token * 1_000_000_000_000)),
            completion_micro_per_m=int(round(completion_per_token * 1_000_000_000_000)),
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
