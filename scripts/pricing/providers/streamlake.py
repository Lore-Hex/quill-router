"""StreamLake KAT pricing plus an hourly account-health canary."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import ProviderPricingResult, fetch_provider
from scripts.pricing.manifest import (
    set_manifest_canary_state,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import probe_openai_chat

SLUG = "streamlake"
BASE_URL = "https://vanchin.streamlake.ai/api/gateway/v1/endpoints"
URL = "https://r.jina.ai/https://www.streamlake.ai/document/DOC/mgrnm4xm362hvp5wyce"
JINA_HEADERS = {"X-Return-Format": "markdown"}
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "streamlake.json"
)
_NATIVE_TO_MODEL_ID = {
    "kat-coder-pro-v2.5": "kwaipilot/kat-coder-pro-v2.5",
    "kat-coder-air-v2.5": "kwaipilot/kat-coder-air-v2.5",
    "kat-coder-pro-v2": "kwaipilot/kat-coder-pro-v2",
}
EXPECTED_MODELS = list(_NATIVE_TO_MODEL_ID.values())
UPSTREAM_ID_MAP = {
    model_id: native_id for native_id, model_id in _NATIVE_TO_MODEL_ID.items()
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}
_LIVE_CANARY_OK = False


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS, _LIVE_CANARY_OK  # noqa: PLW0603

    pricing = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
    _LIVE_CANARY_OK = probe_openai_chat(
        base_url=BASE_URL,
        api_key=os.environ.get("STREAMLAKE_API_KEY"),
        model="kat-coder-pro-v2",
    )
    _DISCOVERED_MANIFEST_ROWS = {
        model_id: {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": native_id,
            "context_length": 262_144,
            "endpoints": ["chat/completions"],
        }
        for native_id, model_id in _NATIVE_TO_MODEL_ID.items()
        if model_id in pricing.prices
    }
    notes = list(pricing.notes)
    notes.append("manual funding required; provider has no autopay")
    notes.append(f"account canary {'passed' if _LIVE_CANARY_OK else 'failed; routes remain dark'}")
    return ProviderPricingResult(
        slug=SLUG,
        prices=pricing.prices,
        source=pricing.source,
        fetched_url="https://www.streamlake.ai/document/DOC/mgrnm4xm362hvp5wyce",
        notes=notes,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    notes = write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=BASE_URL,
    )
    set_manifest_canary_state(MANIFEST_PATH, healthy=_LIVE_CANARY_OK)
    return notes
