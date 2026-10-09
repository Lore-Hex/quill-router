"""Chutes confidential inference catalog and pricing.

Chutes publishes exact model IDs, context, feature metadata, and USD-per-million
token prices from its authenticated OpenAI-compatible ``/models`` endpoint.
Only TEE rows are published by this adapter.
"""

from __future__ import annotations

import os
import re
from decimal import ROUND_HALF_UP, Decimal, DecimalException
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
from scripts.pricing.model_ids import canonicalize_native_model_id

SLUG = "chutes"
URL = "https://llm.chutes.ai/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "chutes.json"
)
EXPECTED_MODELS = ["z-ai/glm-5.2", "moonshotai/kimi-k2.6"]

_NATIVE_TO_MODEL_ID = {
    "Qwen/Qwen3-32B-TEE": "qwen/qwen3-32b",
    "google/gemma-4-31B-turbo-TEE": "google/gemma-4-31b-turbo",
    "Qwen/Qwen3.6-27B-TEE": "qwen/qwen3.6-27b",
    "moonshotai/Kimi-K2.6-TEE": "moonshotai/kimi-k2.6",
    "deepseek-ai/DeepSeek-V3.2-TEE": "deepseek/deepseek-v3.2",
    "Qwen/Qwen3.5-397B-A17B-TEE": "qwen/qwen3.5-397b-a17b",
    "zai-org/GLM-5.2-TEE": "z-ai/glm-5.2",
    "zai-org/GLM-5.1-TEE": "z-ai/glm-5.1",
    "moonshotai/Kimi-K2.5-TEE": "moonshotai/kimi-k2.5",
    "Qwen/Qwen3-235B-A22B-Thinking-2507-TEE": (
        "qwen/qwen3-235b-a22b-thinking-2507"
    ),
    "MiniMaxAI/MiniMax-M2.5-TEE": "minimax/minimax-m2.5",
    "unsloth/Mistral-Nemo-Instruct-2407-TEE": "mistralai/mistral-nemo",
    "zai-org/GLM-5-TEE": "z-ai/glm-5",
    "Nemotron-3-Nano-Omni-30B-TEE": "nvidia/nemotron-3-nano-omni",
}
UPSTREAM_ID_MAP = {
    model_id: native_id for native_id, model_id in _NATIVE_TO_MODEL_ID.items()
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
# Catalog presence is not proof of runtime TEE eligibility. Confirmed failures
# can be held here without discovery or delist/relist recovery clearing them.
_OPERATOR_HOLD_REASONS = {
    # Repeated encrypted probes fail to obtain evidence (upstream HTTP 400).
    "qwen/qwen3-235b-a22b-thinking-2507": "attestation-evidence-unavailable",
}


def _canonical_model_id(native_id: str) -> str | None:
    if native_id in _NATIVE_TO_MODEL_ID:
        return _NATIVE_TO_MODEL_ID[native_id]
    # Only strip Chutes' terminal deployment marker, never model variants.
    # The caller gates on confidential_compute; upstream keeps the exact ID.
    model_id = native_id.removesuffix("-TEE")
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*", model_id) is None:
        return None
    return canonicalize_native_model_id(model_id)


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[call-overload]
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _dollars_per_m_to_micro_per_m(value: object) -> int | None:
    try:
        parsed = Decimal(str(value))
        if not parsed.is_finite() or parsed < 0:
            return None
        return int((parsed * Decimal("1000000")).to_integral_value(ROUND_HALF_UP))
    except (DecimalException, OverflowError, TypeError, ValueError):
        return None


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    UPSTREAM_ID_MAP.clear()
    UPSTREAM_ID_MAP.update(
        {model_id: native_id for native_id, model_id in _NATIVE_TO_MODEL_ID.items()}
    )
    api_key = os.environ.get("CHUTES_API_KEY")
    if not api_key:
        raise RuntimeError("CHUTES_API_KEY is required for Chutes catalog discovery")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": PROVIDER_FETCH_UA,
    }
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=False,
        transport=transport,
    ) as client:
        response = client.get(URL, headers=headers)
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        rows = []
    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for source in rows:
        if not isinstance(source, dict) or source.get("confidential_compute") is not True:
            continue
        native_id = source.get("id")
        if not isinstance(native_id, str):
            continue
        model_id = _canonical_model_id(native_id)
        if model_id is None:
            continue
        previous = discovered.get(model_id)
        if previous is not None and previous["upstream_id"] != native_id:
            raise RuntimeError(f"Chutes catalog has ambiguous upstream IDs for {model_id}")
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": native_id.removesuffix("-TEE"),
            "confidential_compute": True,
        }
        context_length = _positive_int(
            source.get("context_length") or source.get("max_model_len")
        )
        if context_length is not None:
            row["context_length"] = context_length
        max_output = _positive_int(source.get("max_output_length"))
        if max_output is not None:
            row["max_output_tokens"] = max_output
        for field in ("input_modalities", "output_modalities", "supported_features"):
            value = source.get(field)
            if isinstance(value, list):
                row[field] = [str(item) for item in value]
        discovered[model_id] = row

        pricing = source.get("pricing")
        if not isinstance(pricing, dict):
            continue
        prompt = _dollars_per_m_to_micro_per_m(pricing.get("prompt"))
        completion = _dollars_per_m_to_micro_per_m(pricing.get("completion"))
        if prompt is None or completion is None:
            continue
        cached = _dollars_per_m_to_micro_per_m(pricing.get("input_cache_read"))
        prices[model_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cached,
        )

    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    _DISCOVERED_MANIFEST_ROWS = discovered
    UPSTREAM_ID_MAP.update(
        {model_id: row["upstream_id"] for model_id, row in discovered.items()}
    )
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[f"discovered {len(discovered)} confidential-compute models"],
        price_index_model_ids=frozenset(prices.keys() - _OPERATOR_HOLD_REASONS.keys()),
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
        operator_hold_reasons=_OPERATOR_HOLD_REASONS,
    )
