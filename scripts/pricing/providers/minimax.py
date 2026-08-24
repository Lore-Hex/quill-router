"""MiniMax first-party API pricing refresh."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ProviderPricingResult,
    fetch_json,
    fetch_provider,
    guard_manifest_prune,
    reconcile_manifest_tombstones,
)

SLUG = "minimax"
URL = "https://platform.minimax.io/docs/guides/pricing-paygo.md"
MODELS_URL = "https://api.minimax.io/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "minimax.json"
)

EXPECTED_MODELS = [
    "minimax/minimax-m3",
    "minimax/minimax-m2.7",
    "minimax/minimax-m2.7-highspeed",
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


def _known_manifest_model_ids() -> set[str]:
    if not MANIFEST_PATH.exists():
        return set()
    try:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return set()
    return {
        model_id
        for row in rows
        if isinstance(row, dict) and isinstance((model_id := row.get("id")), str) and model_id
    }


def _live_model_rows() -> dict[str, dict[str, Any]]:
    api_key = os.environ.get("MINIMAX_API_KEY") or os.environ.get("MINIMAX_TOKEN_PLAN_API_KEY")
    if not api_key:
        raise RuntimeError("minimax: MINIMAX_API_KEY is required")
    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    source_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        raise RuntimeError("minimax: /v1/models response has no data list")

    discovered: dict[str, dict[str, Any]] = {}
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        native_id = source.get("id")
        if not isinstance(native_id, str) or not native_id:
            continue
        status = source.get("status")
        if isinstance(status, int) and not isinstance(status, bool) and status != 1:
            continue
        model_id = f"minimax/{native_id.casefold()}"
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": str(source.get("display_name") or source.get("name") or native_id),
            "title": str(source.get("title") or native_id),
            "model_type": "chat",
            "input_modalities": ["text"],
            "output_modalities": ["text"],
            "endpoints": ["chat/completions"],
            "status": 1,
        }
        for field, value in (
            ("created", source.get("created")),
            ("context_length", source.get("context_length")),
            ("max_output_tokens", source.get("max_output_tokens")),
        ):
            parsed = _positive_int(value)
            if parsed is not None:
                row[field] = parsed
        discovered[model_id] = row
    if not discovered:
        raise RuntimeError("minimax: /v1/models returned no active models")
    return discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    discovered = _live_model_rows()
    required = frozenset(discovered) - _known_manifest_model_ids()
    result = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        required_models=required,
    )
    result.prices = {
        model_id: price for model_id, price in result.prices.items() if model_id in discovered
    }
    result.source = "api"
    _DISCOVERED_MANIFEST_ROWS = discovered
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("minimax manifest has no models list")

    if not _DISCOVERED_MANIFEST_ROWS:
        raise RuntimeError("minimax manifest refresh has no live discovery rows")
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
            if len(price.tiers) > 1:
                row["price_tiers"] = [
                    {
                        "max_prompt_tokens": price_tier.max_prompt_tokens,
                        "input_token_price_per_m": price_tier.prompt_micro_per_m,
                        "output_token_price_per_m": price_tier.completion_micro_per_m,
                        "cached_input_token_price_per_m": (price_tier.prompt_cached_micro_per_m),
                    }
                    for price_tier in price.tiers
                ]
                row.pop("cached_input_token_price_per_m", None)
            elif tier.prompt_cached_micro_per_m is not None:
                row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
                row.pop("price_tiers", None)
            else:
                row.pop("cached_input_token_price_per_m", None)
                row.pop("price_tiers", None)
            updated.append(model_id)
        present_rows[model_id] = row

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"minimax manifest did not update expected model(s): {missing}")

    rebuilt = reconcile_manifest_tombstones(
        rows,
        present_rows,
        priced_ids=set(result.prices),
        source=result.source,
    )
    guarded = guard_manifest_prune(rows, rebuilt, provider_slug=SLUG)
    if guarded is rows:
        return ["minimax: kept old manifest (mass-prune guard)"]

    raw["source"] = MODELS_URL
    raw["pricing_source"] = URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(guarded)
    raw["models"] = guarded
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [f"minimax: refreshed provider_models/minimax.json ({len(updated)} priced rows)"]
