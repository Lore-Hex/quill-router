"""FriendliAI — human-only provider config.

Friendli publishes an OpenAI-compatible Model API at
https://api.friendli.ai/serverless/v1. Its `/models` response includes
model ids, context length, and per-million-token pricing. The response uses
mixed namespaces:

* GLM/Qwen/MiniMax/DeepSeek use upstream-author ids such as
  `zai-org/GLM-5.2`.
* Llama rows use Friendli-local ids such as
  `meta-llama-3.3-70b-instruct`.

This adapter keeps exact upstream ids in `UPSTREAM_ID_MAP` so the gateway
can call Friendli without falling through to a lowercase/author-stripping
guess.
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
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id

SLUG = "friendli"
URL = "https://api.friendli.ai/serverless/v1/models"

EXPECTED_MODELS = [
    "z-ai/glm-5.2",
]

_NATIVE_TO_OR_ID = {
    "meta-llama-3.3-70b-instruct": "meta-llama/llama-3.3-70b-instruct",
    "meta-llama-3.1-8b-instruct": "meta-llama/llama-3.1-8b-instruct",
    "Qwen/Qwen3-235B-A22B-Instruct-2507": "qwen/qwen3-235b-a22b-2507",
    "LGAI-EXAONE/K-EXAONE-236B-A23B": "lgai-exaone/k-exaone-236b-a23b",
    "zai-org/GLM-5": "z-ai/glm-5",
    "MiniMaxAI/MiniMax-M2.5": "minimax/minimax-m2.5",
    "deepseek-ai/DeepSeek-V3.2": "deepseek/deepseek-v3.2",
    "zai-org/GLM-5.1": "z-ai/glm-5.1",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
}

UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}


def _price_to_micro_per_m(value: object) -> int | None:
    """Friendli returns dollars per million tokens, not dollars per token."""
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return int(round(parsed * 1_000_000))


def fetch() -> ProviderPricingResult:
    api_key = os.environ.get("FRIENDLI_API_KEY")
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
        pricing = row.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        prompt = _price_to_micro_per_m(pricing.get("input") or pricing.get("prompt"))
        completion = _price_to_micro_per_m(
            pricing.get("output") or pricing.get("completion")
        )
        if prompt is None or completion is None:
            continue
        cache_read = _price_to_micro_per_m(pricing.get("input_cache_read"))
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
