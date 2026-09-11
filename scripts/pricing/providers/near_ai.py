"""NEAR AI confidential direct-endpoint catalog and pricing.

NEAR exposes prices from its authenticated cloud catalog and publishes the
model-to-direct-domain registry separately. TrustedRouter intersects those two
sources with the exact workloads pinned by the enclave release. A new catalog
row alone can therefore never create an unverified route.
"""

from __future__ import annotations

import json
import os
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
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
from scripts.pricing.model_ids import remember_upstream_id
from trusted_router.provider_lifecycle import provider_model_retired

SLUG = "near-ai"
CATALOG_URL = "https://cloud-api.near.ai/v1/models"
ENDPOINTS_URL = "https://completions.near.ai/endpoints"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "near-ai.json"
)
MANIFEST_STALE_FALLBACK = True
INCLUDE_IN_PRICE_INDEX = True

# Native ID -> (TR canonical ID, pinned direct TLS domain). This table mirrors
# the enclave's release policy intentionally: catalog discovery cannot make a
# new workload routable before its full TDX/GPU/deployment evidence is reviewed.
_VERIFIED_DIRECT_MODELS = {
    "deepseek-ai/DeepSeek-V4-Flash": (
        "deepseek/deepseek-v4-flash",
        "dsv4-flash.completions.near.ai",
    ),
    "google/gemma-4-31B-it": (
        "google/gemma-4-31b-it",
        "gemma-4-31b.completions.near.ai",
    ),
    "zai-org/GLM-5.1-FP8": ("z-ai/glm-5.1", "glm-5-1.completions.near.ai"),
    "z-ai/glm-5.2": ("z-ai/glm-5.2", "glm-5-2.completions.near.ai"),
    "z-ai/glm-5.3-flash": ("z-ai/glm-5.3-flash", "glm-5-3-flash.completions.near.ai"),
    "openai/gpt-oss-120b": (
        "openai/gpt-oss-120b",
        "gpt-oss-120b.completions.near.ai",
    ),
    "Qwen/Qwen3.6-27B-FP8": (
        "qwen/qwen3.6-27b",
        "qwen3-6-27b.completions.near.ai",
    ),
    "Qwen/Qwen3.6-35B-A3B-FP8": (
        "qwen/qwen3.6-35b-a3b",
        "qwen3-6-35b.completions.near.ai",
    ),
    "Qwen/Qwen3.8-27B": (
        "qwen/qwen3.8-27b",
        "qwen3-8-27b.completions.near.ai",
    ),
    "Qwen/Qwen3-VL-30B-A3B-Instruct": (
        "qwen/qwen3-vl-30b-a3b-instruct",
        "qwen3-vl-30b.completions.near.ai",
    ),
    "Qwen/Qwen3.5-122B-A10B": (
        "qwen/qwen3.5-122b-a10b",
        "qwen35-122b.completions.near.ai",
    ),
}

# A retired flagship must not prevent other release-pinned models refreshing.
EXPECTED_MODELS: list[str] = []
UPSTREAM_ID_MAP = {
    canonical: native for native, (canonical, _domain) in _VERIFIED_DIRECT_MODELS.items()
}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}

# September 11 direct Intel PCS verification: these hosts report an OutOfDate
# TDX module. Catalog/pricing success cannot clear an attestation safety hold.
_OPERATOR_HOLD_REASONS = dict.fromkeys(
    ("qwen/qwen3.6-35b-a3b", "qwen/qwen3.8-27b", "qwen/qwen3-vl-30b-a3b-instruct"),
    "attestation-tcb-out-of-date",
)
# The direct TLS endpoint stopped handshaking during the same incident.
_OPERATOR_HOLD_REASONS["deepseek/deepseek-v4-flash"] = "direct-endpoint-unavailable"


def canonical_model_id(native_id: str) -> str | None:
    """Discover only active, release-pinned direct workloads."""

    native_id = native_id.strip()
    entry = _VERIFIED_DIRECT_MODELS.get(native_id)
    if entry is None or provider_model_retired(SLUG, entry[0], native_id):
        return None
    return entry[0]


def _microdollars_per_million_from_per_token(value: object) -> int | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return int((parsed * Decimal("1000000000000")).to_integral_value(ROUND_HALF_UP))


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _direct_domains(payload: object) -> dict[str, str]:
    rows = payload.get("endpoints") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("near-ai: direct endpoint registry returned an unexpected shape")
    domains: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        domain = row.get("domain")
        models = row.get("models")
        if (
            not isinstance(domain, str)
            or domain != domain.lower()
            or not domain.endswith(".completions.near.ai")
            or any(char in domain for char in "/:@")
            or not isinstance(models, list)
        ):
            continue
        for native_id in models:
            if isinstance(native_id, str):
                domains[native_id] = domain
    return domains


def _modalities(source: dict[str, Any], key: str, fallback: list[str]) -> list[str]:
    value = source.get(key)
    if not isinstance(value, list):
        architecture = source.get("architecture")
        camel = "inputModalities" if key == "input_modalities" else "outputModalities"
        value = architecture.get(camel) if isinstance(architecture, dict) else None
    if not isinstance(value, list):
        return fallback
    normalized = [str(item).lower() for item in value if str(item).strip()]
    return normalized or fallback


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    _DISCOVERED_MANIFEST_ROWS = {}
    api_key = os.environ.get("NEAR_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("near-ai: NEAR_API_KEY is required for catalog discovery")
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        # The authenticated catalog has one pinned authority. A redirect is
        # provider drift, not something the refresh job should follow while
        # carrying a production bearer credential.
        follow_redirects=False,
        transport=transport,
        headers={"Accept": "application/json", "User-Agent": PROVIDER_FETCH_UA},
    ) as client:
        catalog_response = client.get(
            CATALOG_URL,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        catalog_response.raise_for_status()
        endpoints_response = client.get(ENDPOINTS_URL)
        endpoints_response.raise_for_status()

    payload = catalog_response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("near-ai: authenticated model catalog returned an unexpected shape")
    domains = _direct_domains(endpoints_response.json())
    active_direct_models = {
        native_id: entry
        for native_id, entry in _VERIFIED_DIRECT_MODELS.items()
        if not provider_model_retired(SLUG, entry[0], native_id)
    }

    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    notes: list[str] = []
    for source in rows:
        if not isinstance(source, dict) or source.get("owned_by") != "nearai":
            continue
        native_id = source.get("id")
        if not isinstance(native_id, str) or native_id not in active_direct_models:
            continue
        model_id, pinned_domain = active_direct_models[native_id]
        if domains.get(native_id) != pinned_domain:
            notes.append(f"direct endpoint mismatch for {native_id}")
            continue
        pricing = source.get("pricing")
        if not isinstance(pricing, dict):
            continue
        prompt = _microdollars_per_million_from_per_token(pricing.get("prompt"))
        completion = _microdollars_per_million_from_per_token(pricing.get("completion"))
        if prompt is None or completion is None:
            continue
        cached = _microdollars_per_million_from_per_token(pricing.get("input_cache_read"))
        prices[model_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cached,
        )
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": native_id,
            "confidential_compute": True,
            "input_modalities": _modalities(source, "input_modalities", ["text"]),
            "output_modalities": _modalities(source, "output_modalities", ["text"]),
            "endpoints": ["chat/completions"],
            "status": 1,
        }
        context_length = _positive_int(source.get("context_length"))
        if context_length is not None:
            row["context_length"] = context_length
        discovered[model_id] = row

    # Explicitly hold missing/invalid rows. Omitting them would leave stale
    # prices routable during the shared writer's tombstone grace period.
    for native_id, (model_id, pinned_domain) in active_direct_models.items():
        if model_id in prices:
            continue
        reason = (
            "price-unavailable" if domains.get(native_id) == pinned_domain
            else "direct-endpoint-mismatch"
        )
        discovered[model_id] = {
            "id": model_id, "upstream_id": native_id,
            "routable": False, "routable_reason": reason,
        }
        notes.append(f"held {native_id}: {reason}")

    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    _DISCOVERED_MANIFEST_ROWS = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=CATALOG_URL,
        notes=[f"priced {len(prices)} release-pinned direct models", *notes],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    discovered = {key: dict(row) for key, row in _DISCOVERED_MANIFEST_ROWS.items()}
    if MANIFEST_PATH.exists():
        for old in json.loads(MANIFEST_PATH.read_text())["models"]:
            model_id = old.get("id")
            if model_id in result.prices and old.get("routable_reason") == "direct-endpoint-mismatch":
                discovered[model_id]["routable"] = True
    holds = {
        model_id: row["routable_reason"] for model_id, row in discovered.items()
        if row.get("routable") is False
    }
    holds.update(_OPERATOR_HOLD_REASONS)
    holds.update({
        model_id: "retired-upstream"
        for native_id, (model_id, _domain) in _VERIFIED_DIRECT_MODELS.items()
        if provider_model_retired(SLUG, model_id, native_id)
    })
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=discovered,
        operator_hold_reasons=holds,
        source_url=ENDPOINTS_URL,
        pricing_source_url=CATALOG_URL,
    )
