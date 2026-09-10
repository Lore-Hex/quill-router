"""DeepSeek authenticated availability plus official token pricing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_json,
    fetch_provider,
    runtime_required_models,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import price_aliases_for_versioned_families, remember_upstream_id
from scripts.pricing.openai_catalog import discover_available_priced_chat_catalog
from trusted_router.provider_lifecycle import deepseek_off_peak_price, provider_model_retired

SLUG = "deepseek"
URL = "https://api-docs.deepseek.com/quick_start/pricing/"
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
    "deepseek-flash": "deepseek/deepseek-flash",
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
    UPSTREAM_ID_MAP.clear()
    required_models = frozenset(
        model for model in (
            _PERSISTED_VERSIONED_MODELS | runtime_required_models(SLUG)
            | frozenset(EXPECTED_MODELS) | {"deepseek/deepseek-flash"}
        )
        if not provider_model_retired(SLUG, model)
    )
    price_aliases = price_aliases_for_versioned_families(
        required_models,
        _VERSIONED_PRICE_FAMILIES,
    )
    for model_id in price_aliases:
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, "deepseek-v4-flash")
    price_aliases.update({
        "deepseek/deepseek-flash": "deepseek/deepseek-v4-flash",
        "deepseek/deepseek-v4-flash": "deepseek/deepseek-flash",
        "deepseek/deepseek-v4-pro-0813": "deepseek/deepseek-v4-pro",
    })
    result = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        required_models=required_models,
        required_model_price_aliases=price_aliases,
        require_runtime_models=False,
    )
    # The live API renamed Flash before the pricing page. A tiny direct probe
    # verified that deepseek-v4-flash returns model=deepseek-flash (2026-09-10).
    # This is a rolling route, not evidence of any immutable release's weights.
    for model_id in {*result.prices, "deepseek/deepseek-flash"}:
        if provider_model_retired(SLUG, model_id):
            result.prices.pop(model_id, None)
            continue
        price = deepseek_off_peak_price(model_id)
        if price is not None:
            result.prices[model_id] = ModelPrice(
                price.prompt_microdollars_per_million_tokens,
                price.completion_microdollars_per_million_tokens,
                prompt_cached_micro_per_m=price.prompt_cached_microdollars_per_million_tokens,
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
    if any(row.get("id") == "deepseek-flash" for row in rows) and not any(
        row.get("id") == "deepseek-v4-flash" for row in rows
    ):
        # Retain the separately verified backwards-compatible rolling API ID.
        rows.append({"id": "deepseek-v4-flash"})
    discovered = discover_available_priced_chat_catalog(
        rows,
        prices=result.prices,
        explicit_map=_NATIVE_TO_MODEL_ID,
        upstream_id_map=UPSTREAM_ID_MAP,
    )
    if not discovered:
        raise RuntimeError("deepseek: no priced chat models found in authenticated catalog")
    # The official model-details table documents these shared chat capabilities
    # and 1M context; /models currently returns only id/object/owned_by. Without
    # this metadata the renamed Flash row would lose tools and advertise 0 context.
    for row in discovered.values():
        if row["id"] not in {
            "deepseek/deepseek-flash", "deepseek/deepseek-v4-flash", "deepseek/deepseek-v4-pro",
        }:
            continue
        row.setdefault("context_length", 1_048_576)
        row["supported_features"] = ["function-calling", "json-mode", "reasoning-effort"]
        if row["id"] == "deepseek/deepseek-flash":
            row["display_name"] = "DeepSeek Flash (rolling)"
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
