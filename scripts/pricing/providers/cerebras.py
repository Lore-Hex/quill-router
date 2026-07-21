"""Cerebras public model catalog and pricing.

Cerebras publishes an unauthenticated, machine-readable catalog. It is the
source of truth for availability, exact upstream IDs, capabilities, and
per-token prices. Rebuilding the provider manifest from that feed lets hourly
refreshes add new Cerebras models instead of merely updating a hand-maintained
allowlist.
"""

from __future__ import annotations

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
from scripts.pricing.model_ids import remember_upstream_id
from scripts.pricing.openai_catalog import openai_model_price, positive_int

SLUG = "cerebras"
URL = "https://api.cerebras.ai/public/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "cerebras.json"
)

_CANONICAL_BY_NATIVE = {
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "zai-glm-4.7": "z-ai/glm-4.7",
    "gemma-4-31b": "google/gemma-4-31b-it",
}
_ALIASES_BY_NATIVE = {
    "gpt-oss-120b": ("cerebras/gpt-oss-120b",),
    "zai-glm-4.7": ("cerebras/zai-glm-4.7",),
    "gemma-4-31b": ("cerebras/gemma-4-31b",),
}
EXPECTED_MODELS = list(_CANONICAL_BY_NATIVE.values())
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _capability_features(row: dict[str, Any]) -> list[str]:
    capabilities = row.get("capabilities")
    if not isinstance(capabilities, dict):
        return ["high-throughput"]
    normalized = {
        "function_calling": "function-calling",
        "structured_outputs": "structured-outputs",
        "parallel_tool_calls": "parallel-tool-calls",
    }
    features = [
        normalized.get(key, key.replace("_", "-"))
        for key, supported in capabilities.items()
        if supported is True and key not in {"streaming", "vision"}
    ]
    return sorted({"high-throughput", *features})


def _manifest_row(
    source: dict[str, Any],
    *,
    model_id: str,
    native_id: str,
) -> dict[str, Any]:
    limits = source.get("limits")
    if not isinstance(limits, dict):
        limits = {}
    capabilities = source.get("capabilities")
    vision = isinstance(capabilities, dict) and capabilities.get("vision") is True
    row: dict[str, Any] = {
        "id": model_id,
        "upstream_id": native_id,
        "display_name": str(source.get("name") or native_id),
        "title": native_id,
        "features": _capability_features(source),
        "input_modalities": ["text", "image"] if vision else ["text"],
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
    }
    context_length = positive_int(limits.get("max_context_length"))
    if context_length is not None:
        row["context_length"] = context_length
    max_output = positive_int(limits.get("max_completion_tokens"))
    if max_output is not None:
        row["max_output_tokens"] = max_output
    return row


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(URL, headers=headers)
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("Cerebras public models response has no data list")

    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for source in rows:
        if not isinstance(source, dict) or source.get("deprecated") is True:
            continue
        native_id = source.get("id")
        if not isinstance(native_id, str):
            continue
        canonical_id = _CANONICAL_BY_NATIVE.get(native_id)
        if canonical_id is None:
            continue
        price = openai_model_price(source)
        if price is None:
            continue
        for model_id in (canonical_id, *_ALIASES_BY_NATIVE.get(native_id, ())):
            remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
            prices[model_id] = price
            discovered[model_id] = _manifest_row(
                source,
                model_id=model_id,
                native_id=native_id,
            )

    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    _DISCOVERED_MANIFEST_ROWS = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
