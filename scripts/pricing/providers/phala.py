"""Phala model discovery with route-specific confidentiality boundaries.

Phala runs inference inside Intel TDX + NVIDIA Confidential Compute
TEEs. Most TrustedRouter routes use their GPU-TEE tier (the
`phala/<bare>` model id form). A small explicit allowlist may use an
upstream-author pass-through ID after a direct account canary succeeds.
Those exact routes receive a model/provider privacy override in
catalog_data.py and must never inherit the confidential posture of
`phala/*`. See
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

To add a confidential model, probe /v1/models, find the `phala/<bare>`
row, and add its canonical mapping. A non-phala pass-through route
requires an explicit standard-privacy override and a visible-content
chat canary before it can enter `_STANDARD_PASSTHROUGH_TO_OR_ID`.
"""

from __future__ import annotations

import os
from datetime import datetime
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
from scripts.pricing.openai_catalog import discover_openai_chat_catalog
from trusted_router.provider_lifecycle import (
    provider_model_retired,
    provider_price_microdollars,
)

SLUG = "phala"
URL = "https://api.redpill.ai/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "phala.json"
)

EXPECTED_MODELS = [
    "openai/gpt-oss-120b",
    "deepseek/deepseek-v3.2",
    "z-ai/glm-5",
    "z-ai/glm-5.2",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k3",
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
    "phala/glm-5.2": "z-ai/glm-5.2",
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
_STANDARD_PASSTHROUGH_TO_OR_ID = {
    # Direct visible-content canary passed on 2026-07-29. This is not a
    # phala/* Confidential AI ID, so catalog_data.py forces Standard privacy.
    "moonshotai/kimi-k3": "moonshotai/kimi-k3",
}
_DISCOVERED_ID_MAP = {
    **_NATIVE_TO_OR_ID,
    **_STANDARD_PASSTHROUGH_TO_OR_ID,
}
UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _DISCOVERED_ID_MAP.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _apply_lifecycle_policy(
    prices: dict[str, ModelPrice],
    *,
    at: datetime | str | None = None,
) -> dict[str, ModelPrice]:
    effective = {
        model_id: price
        for model_id, price in prices.items()
        if not provider_model_retired(
            SLUG,
            model_id,
            UPSTREAM_ID_MAP.get(model_id),
            at=at,
        )
    }
    for model_id, existing in tuple(effective.items()):
        announced = provider_price_microdollars(SLUG, model_id, at=at)
        if announced is None:
            continue
        effective[model_id] = ModelPrice(
            announced.prompt_microdollars_per_million_tokens,
            announced.completion_microdollars_per_million_tokens,
            prompt_cached_micro_per_m=existing.tiers[0].prompt_cached_micro_per_m,
        )
    return effective


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("PHALA_CONFIDENTIAL_API_KEY") or os.environ.get("PHALA_API_KEY")
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
    if not isinstance(rows, list):
        raise RuntimeError("phala: /v1/models response has no data list")
    prices, discovered = discover_openai_chat_catalog(
        [row for row in rows if isinstance(row, dict)],
        explicit_map=_DISCOVERED_ID_MAP,
        upstream_id_map=UPSTREAM_ID_MAP,
        include=lambda row: row.get("id") in _DISCOVERED_ID_MAP,
    )

    prices = _apply_lifecycle_policy(prices)
    _DISCOVERED_MANIFEST_ROWS = {
        model_id: row for model_id, row in discovered.items() if model_id in prices
    }
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


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=URL,
    )
