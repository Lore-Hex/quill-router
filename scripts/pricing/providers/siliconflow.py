"""SiliconFlow — human-only provider config.

SiliconFlow's pricing page is a Framer site whose server-rendered model cards
contain the authoritative prices. The parser reads those cards directly.

OpenAI-compatible chat completions at api.siliconflow.com/v1.
"""
from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from scripts.pricing.base import ProviderPricingResult, fetch_json, fetch_provider
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import (
    canonicalize_native_model_id,
    price_aliases_for_versioned_families,
    remember_upstream_id,
)

SLUG = "siliconflow"
URL = "https://www.siliconflow.com/pricing"
MODELS_URL = "https://api.siliconflow.com/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "siliconflow.json"
)

EXPECTED_MODELS: list[str] = []  # parser tolerant of upstream renames
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}

# SiliconFlow's public pricing card remains unversioned while its authenticated
# model catalog exposes dated DeepSeek Flash snapshots. The family relationship
# is human-approved here; only IDs actually present in /v1/models can inherit it.
_VERSIONED_PRICE_FAMILIES = {
    "deepseek/deepseek-v4-flash-": "deepseek/deepseek-v4-flash",
}


def _live_model_rows() -> dict[str, dict[str, Any]]:
    api_key = os.environ.get("SILICON_FLOW_API_KEY") or os.environ.get(
        "SILICONFLOW_API_KEY"
    )
    if not api_key:
        return {}
    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("siliconflow: /v1/models response has no data list")
    discovered: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        canonical_id = canonicalize_native_model_id(native_id)
        if canonical_id is None:
            continue
        discovered[canonical_id] = {
            "id": canonical_id,
            "upstream_id": native_id,
            "display_name": str(row.get("name") or native_id),
            "endpoints": ["chat/completions"],
        }
        remember_upstream_id(UPSTREAM_ID_MAP, canonical_id, native_id)
    return discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    live_rows = _live_model_rows()
    live_model_ids = set(live_rows)
    price_aliases = price_aliases_for_versioned_families(
        live_model_ids,
        _VERSIONED_PRICE_FAMILIES,
    )
    result = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        required_models=frozenset(price_aliases),
        required_model_price_aliases=price_aliases,
    )
    if live_model_ids:
        result.notes.append(
            f"matched pricing against {len(live_model_ids)} authenticated live models"
        )
    _DISCOVERED_MANIFEST_ROWS = live_rows
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        replace(result, source="api"),
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=MODELS_URL,
    )
