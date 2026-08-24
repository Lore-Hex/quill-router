"""DeepSeek authenticated availability plus official token pricing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ProviderPricingResult,
    fetch_json,
    fetch_provider,
    runtime_required_models,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import price_aliases_for_versioned_families, remember_upstream_id
from scripts.pricing.openai_catalog import discover_available_priced_chat_catalog

SLUG = "deepseek"
URL = "https://api-docs.deepseek.com/quick_start/pricing"
MODELS_URL = "https://api.deepseek.com/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "deepseek.json"
)
EXPECTED_MODELS = ["deepseek/deepseek-v4-flash"]
_NATIVE_TO_MODEL_ID = {
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-chat": "deepseek/deepseek-chat",
    "deepseek-reasoner": "deepseek/deepseek-reasoner",
}
UPSTREAM_ID_MAP: dict[str, str] = {}
_VERSIONED_PRICE_FAMILIES = {
    "deepseek/deepseek-v4-flash-": "deepseek/deepseek-v4-flash",
}
_PERSISTED_VERSIONED_MODELS = frozenset({"deepseek/deepseek-v4-flash-0731"})
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    required_models = _PERSISTED_VERSIONED_MODELS | runtime_required_models(SLUG)
    price_aliases = price_aliases_for_versioned_families(
        required_models,
        _VERSIONED_PRICE_FAMILIES,
    )
    for model_id in price_aliases:
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, "deepseek-v4-flash")
    result = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        required_models=required_models,
        required_model_price_aliases=price_aliases,
    )
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        _DISCOVERED_MANIFEST_ROWS = {}
        result.notes.append("DEEPSEEK_API_KEY unavailable; skipped model discovery")
        return result
    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    raw_rows = payload.get("data") if isinstance(payload, dict) else None
    rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    discovered = discover_available_priced_chat_catalog(
        rows,
        prices=result.prices,
        explicit_map=_NATIVE_TO_MODEL_ID,
        upstream_id_map=UPSTREAM_ID_MAP,
    )
    if not discovered:
        raise RuntimeError("deepseek: no priced chat models found in authenticated catalog")
    _DISCOVERED_MANIFEST_ROWS = discovered
    result.source = "api"
    result.fetched_url = MODELS_URL
    result.notes.append(
        f"intersected official pricing with {len(discovered)} authenticated chat models"
    )
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=MODELS_URL,
    )
