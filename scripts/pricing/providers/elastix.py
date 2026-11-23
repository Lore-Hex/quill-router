"""Staged ElastixAI Contract v2 feed; not enrolled in the live refresh."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_json,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.provider_contract_catalog import discover_provider_contract_catalog

SLUG = "elastix"
BASE_URL = "https://api.elastix.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/elastix.json"
_HOLD = "marketplace-onboarding-pending"
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}
UPSTREAM_ID_MAP: dict[str, str] = {}


def discover(payload: object) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or payload.get("contract_version") not in ("2.0", "2.1"):
        raise RuntimeError("elastix: priced Provider Reliability Contract v2 feed required")
    if not isinstance(payload.get("provider"), dict) or payload["provider"].get("id") != SLUG:
        raise RuntimeError("elastix: unexpected provider identity")
    prices, rows = discover_provider_contract_catalog(payload, upstream_id_map={})
    if errors := validate(prices, []):
        raise RuntimeError("elastix: " + "; ".join(errors))
    for model_id, row in rows.items():
        tier = prices[model_id].tiers[0]
        if tier.prompt_cached_micro_per_m is not None:
            if tier.prompt_cached_micro_per_m > tier.prompt_micro_per_m:
                raise RuntimeError("elastix: cached input rate exceeds input rate")
            row["supported_features"].append("prompt_caching")
        # The operator contacts are provided for onboarding, not publication.
        public = row["provider_reliability"].copy()
        public.pop("incident_contact")
        public.pop("support_contact")
        row.update(provider_reliability=public, routable=False, routable_reason=_HOLD)
    return prices, rows


def fetch() -> ProviderPricingResult:
    UPSTREAM_ID_MAP.clear()
    _DISCOVERED_ROWS.clear()
    key = os.environ.get("ELASTIX_API_KEY")
    if not key:
        raise RuntimeError("ELASTIX_API_KEY is required for discovery")
    prices, rows = discover(fetch_json(
        URL, extra_headers={"Authorization": f"Bearer {key}"}, follow_redirects=False,
    ))
    _DISCOVERED_ROWS.update(rows)
    UPSTREAM_ID_MAP.update({model_id: row["upstream_id"] for model_id, row in rows.items()})
    return ProviderPricingResult(slug=SLUG, prices=prices, source="api", fetched_url=URL,
                                 include_in_price_index=False)


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _DISCOVERED_ROWS:
        raise RuntimeError("elastix: successful discovery required before writing")
    return write_discovered_chat_manifest(
        result, manifest_path=MANIFEST_PATH, discovered_rows=_DISCOVERED_ROWS, source_url=URL,
        operator_hold_reasons=dict.fromkeys(_DISCOVERED_ROWS, _HOLD),
    )
