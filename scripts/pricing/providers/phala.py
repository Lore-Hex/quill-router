"""Phala (Confidential AI) — human-only provider config.

Phala runs inference inside Intel TDX + NVIDIA Confidential Compute
TEEs. We route exclusively to their GPU-TEE-attested tier (the
`phala/<bare>` model id form), NOT the upstream pass-through tier
(`openai/gpt-oss-120b`, `anthropic/claude-haiku-4.5`, etc.) which
requires a different — separately-issued — redpill key our TR
account isn't entitled to. See
docs.phala.com/phala-cloud/confidential-ai/confidential-model/confidential-ai-api
for the official model-id convention.

This adapter is API-direct (no HTML scraping, no LLM self-heal):
GET https://api.redpill.ai/v1/models returns every served model
WITH its own `pricing` block (USD/token). For phala-prefixed ids
the block carries the rate the confidential tier charges; we
strip the `phala/` prefix, look up the OR-canonical form in
`_NATIVE_TO_OR_ID`, and emit a ModelPrice for it.

Auth: Bearer token in `PHALA_CONFIDENTIAL_API_KEY` env. Without it
the fetch may still succeed (Phala's /v1/models tolerates anon GET)
but is treated as one failure under MAX_TOLERATED_FAILURES if it
401s for any reason.

To add a new Phala-served model: probe /v1/models, find the
`phala/<bare>` row, and add the `phala/<bare>` → OR-canonical pair
to `_NATIVE_TO_OR_ID`. Refresh.py overlays the price automatically.
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

SLUG = "phala"
URL = "https://api.redpill.ai/v1/models"

EXPECTED_MODELS = [
    "openai/gpt-oss-120b",
    "deepseek/deepseek-v3.2",
    "z-ai/glm-5",
    "moonshotai/kimi-k2.6",
    "google/gemma-3-27b-it",
]


# Phala-native id (`phala/<bare>`) → OR-canonical. Source of truth
# is the live /v1/models response on api.redpill.ai cross-checked
# against the OR snapshot. Add entries when Phala publishes new
# `phala/<bare>` aliases for OR-known models.
_NATIVE_TO_OR_ID = {
    "phala/gpt-oss-120b": "openai/gpt-oss-120b",
    "phala/gpt-oss-20b": "openai/gpt-oss-20b",
    "phala/deepseek-v3.2": "deepseek/deepseek-v3.2",
    "phala/deepseek-chat-v3.1": "deepseek/deepseek-chat-v3.1",
    "phala/gemma-3-27b-it": "google/gemma-3-27b-it",
    "phala/glm-5": "z-ai/glm-5",
    "phala/glm-5.1": "z-ai/glm-5.1",
    "phala/glm-4.7": "z-ai/glm-4.7",
    "phala/glm-4.7-flash": "z-ai/glm-4.7-flash",
    "phala/kimi-k2.5": "moonshotai/kimi-k2.5",
    "phala/kimi-k2.6": "moonshotai/kimi-k2.6",
    "phala/qwen-2.5-7b-instruct": "qwen/qwen-2.5-7b-instruct",
    "phala/qwen2.5-vl-72b-instruct": "qwen/qwen2.5-vl-72b-instruct",
    "phala/qwen3-vl-30b-a3b-instruct": "qwen/qwen3-vl-30b-a3b-instruct",
    "phala/qwen3.5-27b": "qwen/qwen3.5-27b",
    "phala/qwen3.5-397b-a17b": "qwen/qwen3.5-397b-a17b",
    "phala/qwen3-coder-next": "qwen/qwen3-coder-next",
    "phala/qwen3-30b-a3b-instruct-2507": "qwen/qwen3-30b-a3b-instruct-2507",
    "phala/mimo-v2-flash": "xiaomi/mimo-v2-flash",
    "phala/minimax-m2.5": "minimax/minimax-m2.5",
}


def _extract_rates(pricing: object) -> tuple[float, float, float | None] | None:
    """Phala /v1/models pricing block is a flat dict of strings in
    USD/token:
        {"prompt": "0.00000032", "completion": "0.00000048",
         "image": "0", "request": "0",
         "input_cache_reads": "0", "input_cache_writes": "0"}
    Return (prompt_per_token, completion_per_token,
    cached_per_token) or None if structurally broken or zero rate.
    Cached returns None when the published rate is 0 (no discount)
    so downstream pricing doesn't store a literal free cache."""
    if not isinstance(pricing, dict):
        return None
    try:
        prompt = float(pricing.get("prompt") or 0)
        completion = float(pricing.get("completion") or 0)
        cached = float(pricing.get("input_cache_reads") or 0)
    except (TypeError, ValueError):
        return None
    if prompt <= 0 or completion <= 0:
        return None
    return prompt, completion, (cached if cached > 0 else None)


def fetch() -> ProviderPricingResult:
    api_key = os.environ.get("PHALA_CONFIDENTIAL_API_KEY") or os.environ.get(
        "PHALA_API_KEY"
    )
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
        rates = _extract_rates(row.get("pricing"))
        if rates is None:
            continue
        prompt_per_token, completion_per_token, cached_per_token = rates
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=int(round(prompt_per_token * 1_000_000_000_000)),
            completion_micro_per_m=int(round(completion_per_token * 1_000_000_000_000)),
            prompt_cached_micro_per_m=(
                int(round(cached_per_token * 1_000_000_000_000))
                if cached_per_token is not None
                else None
            ),
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
