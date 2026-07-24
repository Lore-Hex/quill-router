"""Baseten — provider-native model catalog and pricing.

Baseten publishes an OpenAI-compatible inference API at
https://inference.baseten.co/v1. Its `/models` response includes exact native
model ids, context lengths, and per-token prices as decimal strings. This
module converts those rates into TrustedRouter's integer microdollars per
million tokens and keeps `UPSTREAM_ID_MAP` in sync so the enclave calls the
provider-native id, not a guessed lowercase slug.
"""

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
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.openai_catalog import discover_openai_chat_catalog

SLUG = "baseten"
URL = "https://inference.baseten.co/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "baseten.json"
)

EXPECTED_MODELS = [
    "z-ai/glm-5.2",
    "z-ai/glm-5.2-fast",
    "moonshotai/kimi-k2.7-code",
    "thinkingmachines/inkling-1m",
]

_NATIVE_TO_OR_ID = {
    "openai/gpt-oss-120b": "openai/gpt-oss-120b",
    "zai-org/GLM-4.7": "z-ai/glm-4.7",
    "moonshotai/Kimi-K2.5": "moonshotai/kimi-k2.5",
    "zai-org/GLM-5": "z-ai/glm-5",
    "nvidia/Nemotron-120B-A12B": "nvidia/nemotron-120b-a12b",
    "zai-org/GLM-5.1": "z-ai/glm-5.1",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "deepseek-ai/DeepSeek-V4-Pro": "deepseek/deepseek-v4-pro",
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B": "nvidia/nemotron-3-ultra-550b-a55b",
    "zai-org/GLM-5.2": "z-ai/glm-5.2",
    "zai-org/GLM-5.2-Fast": "z-ai/glm-5.2-fast",
    "moonshotai/Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
    "thinkingmachines/inkling": "thinkingmachines/inkling-1m",
}

UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("BASETEN_API_KEY")
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
    source_rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    prices, discovered = discover_openai_chat_catalog(
        source_rows,
        explicit_map=_NATIVE_TO_OR_ID,
        upstream_id_map=UPSTREAM_ID_MAP,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered

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


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
