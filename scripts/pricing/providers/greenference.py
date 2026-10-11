"""Greenference's authenticated Catalog v2; native IDs and prices stay exact."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_json, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.openai_catalog import probe_openai_chat
from scripts.pricing.provider_contract_catalog import discover_provider_contract_catalog

SLUG = "greenference"
BASE_URL = "https://llm.eu.greenference.com/trustedrouter/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/greenference.json"
MANIFEST_STALE_FALLBACK = True
_SHARED_IDS = {
    **{f"greenference/{name}": f"qwen/{name}" for name in (
        "qwen3-8b", "qwen3-14b", "qwen3-32b", "qwen3-30b-a3b",
        "qwen3.5-9b", "qwen3.6-27b",
    )},
    "greenference/gpt-oss-20b": "openai/gpt-oss-20b",
    "greenference/gemma-4-31b-it": "google/gemma-4-31b-it",
    "greenference/glm-4.7-flash": "z-ai/glm-4.7-flash",
    "greenference/glm-5.3-flash": "z-ai/glm-5.3-flash",
}
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}


def canonical_model_id(native_id: str) -> str:
    # Unknown contract-compliant models remain discoverable under their native
    # ID. Do not guess another vendor's identity from a display name.
    return _SHARED_IDS.get(native_id, native_id)


def discover(payload: object) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    native_prices, native_rows = discover_provider_contract_catalog(payload, upstream_id_map={})
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for native_id, row in native_rows.items():
        model_id = canonical_model_id(native_id)
        if model_id in rows:
            raise RuntimeError("greenference: duplicate canonical model ID")
        price = native_prices[native_id]
        cached = price.tiers[0].prompt_cached_micro_per_m
        if cached is not None and cached > price.prompt_micro_per_m:
            raise RuntimeError("greenference: cache-read price exceeds input price")
        # Named contacts are onboarding-only, even when included in the API.
        reliability = dict(row.get("provider_reliability") or {})
        reliability.pop("support_contact", None)
        reliability.pop("incident_contact", None)
        rows[model_id] = {**row, "id": model_id, "provider_reliability": reliability}
        if cached is not None:
            rows[model_id]["supported_features"].append("prompt_caching")
        replacement = row.get("replacement_model_id")
        if replacement:
            rows[model_id]["replacement_model_id"] = canonical_model_id(replacement)
        prices[model_id] = price
    if not prices or (errors := validate(prices, [])):
        raise RuntimeError("greenference: no active priced chat models" if not prices else "; ".join(errors))
    return prices, rows


def fetch() -> ProviderPricingResult:
    _DISCOVERED_ROWS.clear()
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("GREENFERENCE_API_KEY")
    if not key:
        raise RuntimeError("GREENFERENCE_API_KEY is required for discovery")
    payload = fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"})
    prices, rows = discover(payload)
    checked = models_requiring_canary(MANIFEST_PATH, rows)
    healthy = {
        model_id for model_id in checked
        if probe_openai_chat(
            base_url=BASE_URL, api_key=key, model=rows[model_id]["upstream_id"],
            max_tokens=512, expected_content="PONG", require_usage=True,
            prompt="Reply with exactly PONG and nothing else.",
        )
    }
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    UPSTREAM_ID_MAP.update({model_id: row["upstream_id"] for model_id, row in rows.items()})
    _DISCOVERED_ROWS.update(rows)
    return ProviderPricingResult(
        slug=SLUG, prices=prices, source="api", fetched_url=URL,
        notes=[f"Catalog v2: {len(rows)} models; {len(healthy)}/{len(checked)} new/held canaries passed"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _DISCOVERED_ROWS:
        raise RuntimeError("greenference: fetch must succeed before writing manifest")
    return write_discovered_chat_manifest(
        result, manifest_path=MANIFEST_PATH, discovered_rows=_DISCOVERED_ROWS, source_url=URL,
    )
