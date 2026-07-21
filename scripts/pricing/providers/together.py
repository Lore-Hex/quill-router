"""Together AI — human-only provider config.

Together has a real JSON pricing API at /v1/models, so this provider
bypasses the parser tier entirely. No HTML scraping, no LLM self-heal —
just hit the API and translate.

`/v1/models` includes models that require a separately provisioned dedicated
endpoint. Publishing all of them as prepaid routes caused deterministic 400s.
The documented `/v1/endpoints?type=serverless` endpoint is therefore the
availability source of truth; prices still come from `/v1/models`.

Both endpoints require an API key (Bearer auth). The workflow can provide one
via the TOGETHER_API_KEY env var. Without it, the fetch returns 401 and
Together is counted as a single failure under MAX_TOLERATED_FAILURES — every
other provider still refreshes.
"""
from __future__ import annotations

import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id
from scripts.pricing.openai_catalog import positive_int

SLUG = "together"
URL = "https://api.together.xyz/v1/models"
SERVERLESS_ENDPOINTS_URL = "https://api.together.xyz/v1/endpoints?type=serverless"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "together.json"
)

# Model IDs we expect Together to expose, in OR-canonical form. Parser
# below translates Together's native IDs to these. These are the
# canaries for hourly pricing drift: if Together renames or drops one,
# the refresh job should keep the previous committed price instead of
# silently deleting the endpoint from the catalog.
EXPECTED_MODELS = [
    "deepseek/deepseek-v4-pro",
    "minimax/minimax-m3",
    "z-ai/glm-5.2",
]


# Together native model id → OR-canonical id. Add/extend as new models
# get keyed providers in catalog.py.
_NATIVE_TO_OR_ID = {
    "meta-llama/Llama-3-8b-chat-hf": "meta-llama/llama-3-8b-chat",
    "meta-llama/Llama-3.1-8B-Instruct-Turbo": "meta-llama/llama-3.1-8b-instruct",
    "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo": "meta-llama/llama-3.1-8b-instruct",
    "meta-llama/Llama-3.1-70B-Instruct-Turbo": "meta-llama/llama-3.1-70b-instruct",
    "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo": "meta-llama/llama-3.1-70b-instruct",
    "meta-llama/Llama-3.3-70B-Instruct-Turbo": "meta-llama/llama-3.3-70b-instruct",
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
    "MiniMaxAI/MiniMax-M2.7": "minimax/minimax-m2.7",
    "MiniMaxAI/MiniMax-M3": "minimax/minimax-m3",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
}

# OR-canonical id -> Together-native id. refresh.py reads this human-only
# map when rebuilding the hourly snapshot so endpoint `model_id` remains
# directly callable by Together after every automated price update.
UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _row_to_micro_per_m(price_per_token: object) -> int | None:
    """Convert Together's USD-per-million value to integer microdollars."""
    if price_per_token is None:
        return None
    try:
        value = Decimal(str(price_per_token))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite() or value < 0:
        return None
    return int(
        (value * Decimal("1000000")).to_integral_value(rounding=ROUND_HALF_UP)
    )


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

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
        models_response = client.get(URL, headers=headers)
        models_response.raise_for_status()
        payload = models_response.json()
        endpoints_response = client.get(SERVERLESS_ENDPOINTS_URL, headers=headers)
        endpoints_response.raise_for_status()
        endpoints_payload = endpoints_response.json()
    endpoint_rows = (
        endpoints_payload.get("data") or []
        if isinstance(endpoints_payload, dict)
        else endpoints_payload
    )
    serverless_model_ids = {
        row.get("model")
        for row in endpoint_rows
        if isinstance(row, dict)
        and row.get("type") == "serverless"
        and row.get("state") == "STARTED"
        and isinstance(row.get("model"), str)
    }
    if isinstance(payload, dict):
        rows = payload.get("data") or []
    else:
        rows = payload
    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("type") != "chat":
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        if native_id not in serverless_model_ids:
            continue
        or_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        if or_id is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, or_id, native_id)
        pricing = row.get("pricing") or {}
        if not isinstance(pricing, dict):
            continue
        prompt = _row_to_micro_per_m(pricing.get("input"))
        completion = _row_to_micro_per_m(pricing.get("output"))
        cached_prompt = _row_to_micro_per_m(pricing.get("cached_input"))
        if prompt is None or completion is None:
            continue
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cached_prompt,
        )
        manifest_row: dict[str, Any] = {
            "id": or_id,
            "upstream_id": native_id,
            "display_name": str(row.get("display_name") or native_id),
            "title": native_id,
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "endpoints": ["chat/completions"],
            "features": ["serverless"],
            "status": 1,
        }
        context_length = positive_int(row.get("context_length"))
        if context_length is not None:
            manifest_row["context_length"] = context_length
        discovered[or_id] = manifest_row

    notes: list[str] = []
    if not prices:
        notes.append(
            "no started Together serverless models matched the TrustedRouter "
            "catalog — check both provider API responses"
        )
    # Validate strictly: Together is a direct JSON API, not a brittle
    # HTML scraper. If a canary model disappears, treat this provider
    # as failed so refresh.py reuses the previous committed prices
    # instead of dropping live endpoints from the catalog.
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")
        raise RuntimeError("; ".join(errors))

    _DISCOVERED_MANIFEST_ROWS = discovered

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=SERVERLESS_ENDPOINTS_URL,
    )
