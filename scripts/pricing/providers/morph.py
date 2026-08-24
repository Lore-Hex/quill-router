"""Morph chat catalog intersected with Morph's official pricing page."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ProviderPricingResult,
    fetch_provider,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import remember_upstream_id

SLUG = "morph"
MODELS_URL = "https://api.morphllm.com/v1/models"
URL = "https://www.morphllm.com/pricing"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "morph.json"
)
_NATIVE_TO_MODEL_ID = {
    "morph-glm52-744b": "z-ai/glm-5.2",
    "morph-qwen35-397b": "qwen/qwen3.5-397b-a17b",
    "morph-qwen36-27b": "qwen/qwen3.6-27b",
    "morph-minimax27-230b": "minimax/minimax-m2.7",
    "morph-minimax3-428b": "minimax/minimax-m3",
    "morph-dsv4flash": "deepseek/deepseek-v4-flash",
    "morph-v3-fast": "morph/morph-v3-fast",
    "morph-v3-large": "morph/morph-v3-large",
}
EXPECTED_MODELS = list(_NATIVE_TO_MODEL_ID.values())
UPSTREAM_ID_MAP = {
    model_id: native_id for native_id, model_id in _NATIVE_TO_MODEL_ID.items()
}
_CONTEXT_LENGTHS = {
    "morph-glm52-744b": 1_048_576,
    "morph-qwen35-397b": 262_144,
    "morph-qwen36-27b": 131_072,
    "morph-minimax27-230b": 196_608,
    "morph-minimax3-428b": 262_144,
    "morph-dsv4flash": 1_048_576,
    "morph-v3-fast": 262_144,
    "morph-v3-large": 262_144,
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    pricing = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        accepted_status_codes=frozenset({429}),
    )
    api_key = os.environ.get("MORPH_API_KEY")
    if not api_key:
        raise RuntimeError("MORPH_API_KEY is required for model discovery")
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(
            MODELS_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "User-Agent": PROVIDER_FETCH_UA,
            },
        )
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("data") if isinstance(payload, dict) else payload
    live_ids = {
        row.get("id")
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(rows, list) else set()
    discovered: dict[str, dict[str, Any]] = {}
    prices = {}
    for native_id, model_id in _NATIVE_TO_MODEL_ID.items():
        if native_id not in live_ids or model_id not in pricing.prices:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        discovered[model_id] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": native_id,
            "context_length": _CONTEXT_LENGTHS[native_id],
            "endpoints": ["chat/completions"],
        }
        prices[model_id] = pricing.prices[model_id]

    _DISCOVERED_MANIFEST_ROWS = discovered
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source=pricing.source,
        fetched_url="https://www.morphllm.com/pricing",
        notes=[f"intersected {len(discovered)} live chat models with official prices"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=MODELS_URL,
    )
