"""Z.AI authenticated availability plus official token pricing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import ProviderPricingResult, fetch_json, fetch_provider
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.openai_catalog import discover_available_priced_chat_catalog

SLUG = "zai"
URL = "https://docs.z.ai/guides/overview/pricing.md"
MODELS_URL = "https://api.z.ai/api/paas/v4/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "zai.json"
)
EXPECTED_MODELS = [
    "z-ai/glm-4.6",
    "z-ai/glm-5.2",
    "z-ai/glm-5.3",
    "z-ai/glm-5.3-flash",
]
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    result = fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
    api_key = os.environ.get("ZAI_API_KEY")
    if not api_key:
        _DISCOVERED_MANIFEST_ROWS = {}
        result.notes.append("ZAI_API_KEY unavailable; skipped model discovery")
        return result
    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    raw_rows = payload.get("data") if isinstance(payload, dict) else None
    rows = [row for row in raw_rows if isinstance(row, dict)] if isinstance(raw_rows, list) else []
    explicit_map = {
        str(row["id"]): f"z-ai/{str(row['id']).casefold()}"
        for row in rows
        if isinstance(row.get("id"), str)
    }
    discovered = discover_available_priced_chat_catalog(
        rows,
        prices=result.prices,
        explicit_map=explicit_map,
        upstream_id_map=UPSTREAM_ID_MAP,
    )
    flash = discovered.get("z-ai/glm-5.3-flash")
    if flash is not None:
        # Z.AI's launch documentation is authoritative for the first native
        # multimodal GLM-5 model. Its bare /models row currently contains only
        # the ID, so preserve these capabilities explicitly until the API feed
        # exposes them itself.
        flash["context_length"] = 1_048_576
        flash["max_output_tokens"] = 131_072
        flash["input_modalities"] = ["text", "image", "video"]
        flash["output_modalities"] = ["text"]
        flash["supported_features"] = [
            "reasoning",
            "structured_outputs",
            "tools",
            "prompt_cache",
        ]
    if not discovered:
        raise RuntimeError("zai: no priced chat models found in authenticated catalog")
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
