"""Muna's authoritative, authenticated Provider Reliability Contract v2 catalog."""

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

SLUG = "muna"
BASE_URL = "https://inference.muna.ai/v1"
URL = "https://inference.muna.ai/integrations/trustedrouter/catalog"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/muna.json"
MANIFEST_STALE_FALLBACK = True
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}


def canonical_model_id(native_id: str) -> str:
    return {"qwen/qwen-3.8-27b": "qwen/qwen3.8-27b"}.get(native_id, native_id)


def discover(payload: object) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or payload.get("contract_version") != "2.0":
        raise RuntimeError("muna: a Contract v2 catalog is required")
    if not isinstance(payload.get("provider"), dict) or payload["provider"].get("id") != SLUG:
        raise RuntimeError("muna: unexpected catalog provider")
    native_prices, native_rows = discover_provider_contract_catalog(payload, upstream_id_map={})
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for native_id, native_row in native_rows.items():
        model_id = canonical_model_id(native_id)
        if model_id in rows:
            raise RuntimeError("muna: duplicate canonical model ID")
        price = native_prices[native_id]
        cached = price.tiers[0].prompt_cached_micro_per_m
        if cached is not None and cached > price.prompt_micro_per_m:
            raise RuntimeError("muna: cache-read price exceeds input price")
        reliability = dict(native_row.get("provider_reliability") or {})
        reliability.pop("support_contact", None)
        reliability.pop("incident_contact", None)
        row = {**native_row, "id": model_id, "provider_reliability": reliability}
        if cached is not None:
            row["supported_features"].append("prompt_caching")
        if row.get("replacement_model_id"):
            row["replacement_model_id"] = canonical_model_id(row["replacement_model_id"])
        prices[model_id], rows[model_id] = price, row
    if not prices or (errors := validate(prices, [])):
        raise RuntimeError("muna: no active priced chat models" if not prices else "; ".join(errors))
    return prices, rows


def fetch() -> ProviderPricingResult:
    _DISCOVERED_ROWS.clear()
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("MUNA_API_KEY")
    if not key:
        raise RuntimeError("MUNA_API_KEY is required for discovery")
    prices, rows = discover(fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"}))
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
    _DISCOVERED_ROWS.update(rows)
    UPSTREAM_ID_MAP.update({model_id: row["upstream_id"] for model_id, row in rows.items()})
    return ProviderPricingResult(
        slug=SLUG, prices=prices, source="api", fetched_url=URL,
        notes=[f"Catalog v2: {len(rows)} models; {len(healthy)}/{len(checked)} new/held canaries passed"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _DISCOVERED_ROWS:
        raise RuntimeError("muna: fetch must succeed before writing manifest")
    return write_discovered_chat_manifest(
        result, manifest_path=MANIFEST_PATH, discovered_rows=_DISCOVERED_ROWS, source_url=URL,
    )
