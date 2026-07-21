"""Makora provider-native model and pricing refresh.

Makora's authenticated OpenAI-compatible `/v1/models` feed exposes model IDs,
capabilities, context windows, and the account's billable per-token prices.
Using that single structured source means newly launched priced chat models can
be published without adding a homepage label to a parser first.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_json,
    guard_manifest_prune,
    reconcile_manifest_tombstones,
    validate,
)
from scripts.pricing.model_ids import canonicalize_native_model_id
from scripts.pricing.openai_catalog import dollars_per_token_to_micro_per_m

SLUG = "makora"
URL = "https://www.makora.com/"
MODELS_URL = "https://inference.makora.com/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "makora.json"
)

EXPECTED_MODELS = [
    "deepseek/deepseek-v4-flash",
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.2",
    "z-ai/glm-5.2-nvfp4",
    "moonshotai/kimi-k2.7-code",
    "meta-llama/llama-3.3-70b-instruct",
    "qwen/qwen3.6-27b",
    "qwen/qwen3.6-35b-a3b",
]
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _existing_native_ids() -> tuple[dict[str, str], set[str]]:
    if not MANIFEST_PATH.exists():
        return {}, set()
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, set()
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {}, set()
    native_to_canonical: dict[str, str] = {}
    canonical_ids: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        native_id = row.get("upstream_id")
        if not isinstance(model_id, str):
            continue
        canonical_ids.add(model_id)
        if isinstance(native_id, str):
            native_to_canonical[native_id] = model_id
            native_to_canonical[native_id.casefold()] = model_id
    return native_to_canonical, canonical_ids


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _live_catalog() -> tuple[dict[str, dict[str, Any]], dict[str, ModelPrice]]:
    api_key = os.environ.get("MAKORA_API_KEY") or os.environ.get("MAKORA_OPTIMIZE_TOKEN")
    if not api_key:
        raise RuntimeError("makora: MAKORA_API_KEY is required")
    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    source_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        raise RuntimeError("makora: /v1/models response has no data list")

    existing_ids, _known = _existing_native_ids()
    discovered: dict[str, dict[str, Any]] = {}
    prices: dict[str, ModelPrice] = {}
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        native_id = source.get("id")
        if not isinstance(native_id, str) or not native_id:
            continue
        model_id = (
            existing_ids.get(native_id)
            or existing_ids.get(native_id.casefold())
            or canonicalize_native_model_id(native_id)
        )
        if model_id is None:
            continue
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": str(source.get("name") or native_id),
            "title": str(source.get("name") or native_id),
            "model_type": "chat",
            "input_modalities": _string_list(source.get("input_modalities")) or ["text"],
            "output_modalities": _string_list(source.get("output_modalities")) or ["text"],
            "endpoints": ["chat/completions"],
            "status": 1,
        }
        context_length = _positive_int(source.get("max_model_len") or source.get("context_length"))
        if context_length is not None:
            row["context_length"] = context_length
        max_output_tokens = _positive_int(source.get("max_output_length"))
        if max_output_tokens is not None:
            row["max_output_tokens"] = max_output_tokens
        features = _string_list(source.get("supported_features"))
        if features:
            row["features"] = features
        supported_parameters = _string_list(source.get("supported_sampling_parameters"))
        if supported_parameters:
            row["supported_parameters"] = supported_parameters
        discovered[model_id] = row

        pricing = source.get("pricing")
        if not isinstance(pricing, dict):
            continue
        prompt = dollars_per_token_to_micro_per_m(pricing.get("prompt"))
        completion = dollars_per_token_to_micro_per_m(pricing.get("completion"))
        if prompt is None or completion is None:
            continue
        cached = dollars_per_token_to_micro_per_m(pricing.get("input_cache_read"))
        prices[model_id] = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cached,
        )
    if not discovered:
        raise RuntimeError("makora: /v1/models returned no supported model ids")
    return discovered, prices


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    discovered, prices = _live_catalog()
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    _DISCOVERED_MANIFEST_ROWS = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=MODELS_URL,
        notes=[f"discovered {len(discovered)} live models with {len(prices)} prices"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Update Makora's supplemental runtime manifest from parsed prices.

    The shared hourly snapshot merger updates `openrouter_snapshot.json`.
    Makora's actual runtime routes live in `provider_models/makora.json`, so
    this hook keeps that manifest from becoming a manually maintained price
    island.
    """

    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("makora manifest has no models list")

    if not _DISCOVERED_MANIFEST_ROWS:
        raise RuntimeError("makora manifest refresh has no live discovery rows")
    existing_by_id = {
        row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    present_rows: dict[str, dict[str, Any]] = {}
    updated: list[str] = []
    for model_id, discovered in sorted(_DISCOVERED_MANIFEST_ROWS.items()):
        row = dict(existing_by_id.get(model_id) or {})
        row.update(discovered)
        price = result.prices.get(model_id)
        if price is not None:
            tier = price.tiers[0]
            row["input_token_price_per_m"] = tier.prompt_micro_per_m
            row["output_token_price_per_m"] = tier.completion_micro_per_m
            if tier.prompt_cached_micro_per_m is not None:
                row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
            else:
                row.pop("cached_input_token_price_per_m", None)
            updated.append(model_id)
        present_rows[model_id] = row

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"makora manifest did not update expected model(s): {missing}")
    if not updated:
        raise RuntimeError("makora manifest update touched no rows")

    rebuilt = reconcile_manifest_tombstones(
        rows,
        present_rows,
        priced_ids=set(result.prices),
        source=result.source,
    )
    guarded = guard_manifest_prune(rows, rebuilt, provider_slug=SLUG)
    if guarded is rows:
        return ["makora: kept old manifest (mass-prune guard)"]

    raw["_about"] = (
        "Provider-native supplement for Makora Inference routes. Model IDs, "
        "capabilities, context windows, and account-billable prices refresh "
        "hourly from Makora's authenticated /v1/models feed."
    )
    raw["source"] = MODELS_URL
    raw["pricing_source"] = MODELS_URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(guarded)
    raw["models"] = guarded

    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [f"makora: refreshed provider_models/makora.json ({len(updated)} priced rows)"]
