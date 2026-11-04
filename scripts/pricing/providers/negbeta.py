"""Negbeta's key-scoped Provider Reliability Contract v2 catalog."""

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

SLUG = "negbeta"
BASE_URL = "https://inference.negbeta.com/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/negbeta.json"
MANIFEST_STALE_FALLBACK = True
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}


def canonical_model_id(native_id: str) -> str:
    return native_id


def discover(payload: object) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or payload.get("contract_version") != "2.0":
        raise RuntimeError("negbeta: a Contract v2 catalog is required")
    if not isinstance(payload.get("provider"), dict) or payload["provider"].get("id") != SLUG:
        raise RuntimeError("negbeta: unexpected catalog provider")
    prices, rows = discover_provider_contract_catalog(payload, upstream_id_map={})
    if not prices or (errors := validate(prices, [])):
        raise RuntimeError("negbeta: no active priced chat models" if not prices else "; ".join(errors))
    for model_id, row in rows.items():
        cached = prices[model_id].tiers[0].prompt_cached_micro_per_m
        if cached is not None and cached > prices[model_id].prompt_micro_per_m:
            raise RuntimeError("negbeta: cache-read price exceeds input price")
        reliability = dict(row.get("provider_reliability") or {})
        reliability.pop("support_contact", None)
        reliability.pop("incident_contact", None)
        row["provider_reliability"] = reliability
        if cached is not None:
            row["supported_features"].append("prompt_caching")
    return prices, rows


def fetch() -> ProviderPricingResult:
    _DISCOVERED_ROWS.clear()
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("NEGBETA_API_KEY")
    if not key:
        raise RuntimeError("NEGBETA_API_KEY is required for discovery")
    prices, rows = discover(fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"}))
    checked = models_requiring_canary(MANIFEST_PATH, rows)
    healthy = {
        model_id for model_id in checked
        if probe_openai_chat(
            base_url=BASE_URL, api_key=key, model=rows[model_id]["upstream_id"],
            max_tokens=64, expected_content="PONG", require_usage=True,
            prompt="Reply with exactly PONG and nothing else.",
            extra_body={"reasoning_effort": "none"},
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
        raise RuntimeError("negbeta: fetch must succeed before writing manifest")
    return write_discovered_chat_manifest(
        result, manifest_path=MANIFEST_PATH, discovered_rows=_DISCOVERED_ROWS, source_url=URL,
        # Remove only after the gateway release is independently verified on all clouds.
        operator_hold_reasons={model_id: "gateway-rollout-pending" for model_id in _DISCOVERED_ROWS},
    )
