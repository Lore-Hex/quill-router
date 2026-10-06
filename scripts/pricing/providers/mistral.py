"""Mistral authenticated availability plus official token pricing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import ProviderPricingResult, fetch_json, fetch_provider
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.openai_catalog import discover_available_priced_chat_catalog

SLUG = "mistral"
URL = "https://docs.mistral.ai/inference/pricing"
MODELS_URL = "https://api.mistral.ai/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "mistral.json"
)
EXPECTED_MODELS = ["mistralai/mistral-small-2603"]
_NATIVE_TO_MODEL_ID = {
    "mistral-medium-3.5": "mistralai/mistral-medium-3-5",
    "mistral-medium-3-5": "mistralai/mistral-medium-3-5",
    "mistral-large-latest": "mistralai/mistral-large",
    "devstral-medium-latest": "mistralai/devstral-medium",
    "devstral-latest": "mistralai/devstral-small",
    "magistral-medium-latest": "mistralai/magistral-medium",
    "magistral-small-latest": "mistralai/magistral-small",
}
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _supports_chat(row: dict[str, Any]) -> bool:
    capabilities = row.get("capabilities")
    return isinstance(capabilities, dict) and capabilities.get("completion_chat") is True


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    UPSTREAM_ID_MAP.clear()
    result = fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise RuntimeError("MISTRAL_API_KEY unavailable; cannot verify priced model availability")
    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    raw_rows = payload.get("data") if isinstance(payload, dict) else None
    rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    explicit_map = dict(_NATIVE_TO_MODEL_ID)
    for row in rows:
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        direct_id = f"mistralai/{native_id.casefold()}"
        if direct_id in result.prices:
            explicit_map[native_id] = direct_id
    discovered = discover_available_priced_chat_catalog(
        rows,
        prices=result.prices,
        explicit_map=explicit_map,
        upstream_id_map=UPSTREAM_ID_MAP,
        include=_supports_chat,
    )
    if not discovered:
        raise RuntimeError("mistral: no priced chat models found in authenticated catalog")
    # The snapshot merger also consumes result.prices; do not let a docs-only
    # launch bypass the manifest's authenticated availability intersection.
    result.prices = {model_id: result.prices[model_id] for model_id in discovered}
    by_upstream = {row["id"]: row for row in rows if isinstance(row.get("id"), str)}
    for row in discovered.values():
        capabilities = by_upstream[row["upstream_id"]].get("capabilities", {})
        row["input_modalities"] = ["text", "image"] if capabilities.get("vision") else ["text"]
        row["output_modalities"] = ["text"]
        row["supports_reasoning"] = capabilities.get("reasoning") is True
        row["supported_features"] = (
            ["function-calling"] if capabilities.get("function_calling") is True else []
        )
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
        pricing_source_url=URL,
    )
