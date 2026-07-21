"""Crusoe Managed Inference — provider-native model catalog and pricing.

Crusoe serves an OpenAI-compatible API at
https://api.inference.crusoecloud.com/v1. Its `/models` response includes
exact native model IDs, context lengths, supported parameters, and pricing in
USD per million tokens. This adapter keeps a public TrustedRouter canonical ID
while preserving the exact upstream ID in `UPSTREAM_ID_MAP` so the enclave does
not guess casing for provider calls.
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
from scripts.pricing.manifest import (
    set_manifest_canary_state,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id
from scripts.pricing.openai_catalog import positive_int

SLUG = "crusoe"
URL = "https://api.inference.crusoecloud.com/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "crusoe.json"
)

EXPECTED_MODELS = [
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.6",
    "deepseek/deepseek-v4-flash",
    "openai/gpt-oss-120b",
]

_NATIVE_TO_OR_ID = {
    "deepseek-ai/DeepSeek-V3-0324": "deepseek/deepseek-v3-0324",
    "deepseek-ai/Deepseek-V4-Flash": "deepseek/deepseek-v4-flash",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    "google/gemma-4-31b-it": "google/gemma-4-31b-it",
    "meta-llama/Llama-3.3-70B-Instruct": "meta-llama/llama-3.3-70b-instruct",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B": "nvidia/nemotron-3-nano-30b-a3b",
    "nvidia/Nemotron-3-Nano-Omni-Reasoning-30B-A3B": (
        "nvidia/nemotron-3-nano-omni-reasoning-30b-a3b"
    ),
    "nvidia/NVIDIA-Nemotron-3-Super-120B-A12B": "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B": "nvidia/nemotron-3-ultra-550b",
    "openai/gpt-oss-120b": "openai/gpt-oss-120b",
    "Qwen/Qwen3-235B-A22B-Instruct-2507": "qwen/qwen3-235b-a22b-2507",
    "yutori/n1.5": "yutori/n1.5",
    "zai/GLM-5.1": "z-ai/glm-5.1",
    "zai/GLM-5.2": "z-ai/glm-5.2",
}

UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_LIVE_CANARY_OK = True


def _dollars_per_m_to_micro_per_m(value: object) -> int | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return int((parsed * Decimal("1000000")).to_integral_value(ROUND_HALF_UP))


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS, _LIVE_CANARY_OK  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    api_key = os.environ.get("CRUSOE_API_KEY")
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
        if getattr(response, "status_code", None) in {401, 403}:
            _LIVE_CANARY_OK = False
            return ProviderPricingResult(
                slug=SLUG,
                prices={},
                source="api_auth_failed",
                fetched_url=URL,
                notes=["account authentication failed; routes remain dark"],
            )
        response.raise_for_status()
        payload = response.json()
    _LIVE_CANARY_OK = True

    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = []
    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
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
        prompt = _dollars_per_m_to_micro_per_m(pricing.get("prompt"))
        completion = _dollars_per_m_to_micro_per_m(pricing.get("completion"))
        if prompt is None or completion is None:
            continue
        cache_read = _dollars_per_m_to_micro_per_m(pricing.get("input_cache_reads"))
        prices[or_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cache_read,
        )
        manifest_row: dict[str, Any] = {
            "id": or_id,
            "upstream_id": native_id,
            "display_name": str(row.get("display_name") or row.get("name") or native_id),
            "title": native_id,
            "model_type": "chat",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "endpoints": ["chat/completions"],
            "status": 1,
        }
        context_length = positive_int(row.get("context_length"))
        if context_length is not None:
            manifest_row["context_length"] = context_length
        max_output = positive_int(row.get("max_output_tokens"))
        if max_output is not None:
            manifest_row["max_output_tokens"] = max_output
        supported = row.get("supported_parameters")
        if isinstance(supported, list):
            manifest_row["supported_parameters"] = [
                str(value) for value in supported if isinstance(value, str)
            ]
        discovered[or_id] = manifest_row

    notes: list[str] = []
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
    if not _LIVE_CANARY_OK:
        set_manifest_canary_state(MANIFEST_PATH, healthy=False)
        return ["crusoe: account authentication failed; routes remain dark"]

    notes = write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
    set_manifest_canary_state(MANIFEST_PATH, healthy=True)
    return notes
