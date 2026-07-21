"""Novita pricing plus authenticated live-model discovery.

A route is published only when Novita's authenticated model API says the
operator account can invoke it. The public pricing table remains authoritative
for billing. This intersection lets new launches appear automatically without
guessing capabilities or creating zero-priced routes.
"""

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
    validate,
)
from scripts.pricing.model_ids import (
    canonicalize_native_model_id,
    canonicalize_unqualified_model_id,
)

SLUG = "novita"
URL = "https://novita.ai/pricing"
MODELS_URL = "https://api.novita.ai/openai/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "novita.json"
)

EXPECTED_MODELS = [
    "deepseek/deepseek-v4-flash",
    "moonshotai/kimi-k3",
    "tencent/hy3",
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


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _existing_native_ids() -> dict[str, str]:
    if not MANIFEST_PATH.exists():
        return {}
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return {}
    mapped: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        model_id = row.get("id")
        native_id = row.get("upstream_id") or row.get("title") or model_id
        if not isinstance(model_id, str) or not isinstance(native_id, str):
            continue
        mapped[native_id] = model_id
        mapped[native_id.casefold()] = model_id
    return mapped


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


def _new_required_price_ids(
    discovered: dict[str, dict[str, Any]],
) -> frozenset[str]:
    """Return newly listed, live chat models that need a public price row."""

    known = _known_manifest_model_ids()
    return frozenset(
        model_id
        for model_id, row in discovered.items()
        if model_id not in known and row.get("status") == 1
    )


def _live_model_rows() -> dict[str, dict[str, Any]]:
    api_key = os.environ.get("NOVITA_API_KEY")
    if not api_key:
        raise RuntimeError("novita: NOVITA_API_KEY is required")
    payload = fetch_json(
        MODELS_URL,
        extra_headers={"Authorization": f"Bearer {api_key}"},
    )
    source_rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        raise RuntimeError("novita: /models response has no data list")

    existing_ids = _existing_native_ids()
    discovered: dict[str, dict[str, Any]] = {}
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        native_id = source.get("id")
        if not isinstance(native_id, str) or not native_id:
            continue
        status = source.get("status")
        if isinstance(status, int) and not isinstance(status, bool) and status <= 0:
            continue
        endpoints = _string_list(source.get("endpoints"))
        if "chat/completions" not in endpoints:
            continue
        model_id = (
            existing_ids.get(native_id)
            or existing_ids.get(native_id.casefold())
            or canonicalize_native_model_id(native_id)
            or canonicalize_unqualified_model_id(native_id)
        )
        if model_id is None:
            continue

        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": str(source.get("display_name") or native_id),
            "title": str(source.get("title") or native_id),
            "model_type": str(source.get("model_type") or "chat"),
            "features": _string_list(source.get("features")),
            "input_modalities": _string_list(source.get("input_modalities")) or ["text"],
            "output_modalities": _string_list(source.get("output_modalities")) or ["text"],
            "endpoints": endpoints,
            "status": _positive_int(source.get("status")) or 1,
        }
        optional_ints = {
            "created": source.get("created"),
            "context_length": source.get("context_size") or source.get("context_length"),
            "max_output_tokens": source.get("max_output_tokens"),
        }
        for field, value in optional_ints.items():
            parsed = _positive_int(value)
            if parsed is not None:
                row[field] = parsed
        discovered[model_id] = row

    if not discovered:
        raise RuntimeError("novita: /models returned no supported model ids")
    return discovered


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    discovered = _live_model_rows()
    required_price_ids = _new_required_price_ids(discovered)
    result = fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        required_models=required_price_ids,
    )
    _DISCOVERED_MANIFEST_ROWS = discovered
    result.prices = {
        model_id: price for model_id, price in result.prices.items() if model_id in discovered
    }
    errors = validate(result.prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    result.source = "api"
    result.notes.append(
        f"intersected official prices with {len(discovered)} authenticated live models"
    )
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Reconcile Novita routes against live models and official prices."""

    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("novita manifest has no models list")
    if not _DISCOVERED_MANIFEST_ROWS:
        raise RuntimeError("novita manifest refresh has no authenticated discovery rows")
    scale = raw.get("price_scale_to_microdollars_per_million_tokens", 1)
    if not isinstance(scale, int) or scale <= 0:
        raise RuntimeError("novita manifest has invalid price scale")

    def manifest_units(microdollars: int) -> int:
        value, remainder = divmod(microdollars, scale)
        if remainder:
            raise RuntimeError(
                f"novita price {microdollars} is not representable at manifest scale {scale}"
            )
        return value

    existing_by_id = {
        row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    present_rows: dict[str, dict[str, Any]] = {}
    updated = 0
    appended = 0
    for model_id, discovered in sorted(_DISCOVERED_MANIFEST_ROWS.items()):
        existing = existing_by_id.get(model_id)
        row = dict(existing) if existing is not None else {}
        row.update(discovered)
        price = result.prices.get(model_id)
        if price is not None:
            tier = price.tiers[0]
            row["input_token_price_per_m"] = manifest_units(tier.prompt_micro_per_m)
            row["output_token_price_per_m"] = manifest_units(tier.completion_micro_per_m)
            if tier.prompt_cached_micro_per_m is not None:
                row["cached_input_token_price_per_m"] = manifest_units(
                    tier.prompt_cached_micro_per_m
                )
            else:
                row.pop("cached_input_token_price_per_m", None)
            updated += 1
        if existing is None:
            appended += 1
        present_rows[model_id] = row

    rebuilt = reconcile_manifest_tombstones(
        rows,
        present_rows,
        priced_ids=set(result.prices),
        source=result.source,
    )
    guarded = guard_manifest_prune(rows, rebuilt, provider_slug=SLUG)
    if guarded is rows:
        return ["novita: kept old manifest (mass-prune guard)"]

    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(guarded)
    raw["models"] = guarded
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    suffix = f", appended {appended}" if appended else ""
    return [f"novita: refreshed provider_models/novita.json ({updated} priced rows{suffix})"]
