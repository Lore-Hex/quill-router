"""Shared writers for committed provider-pricing manifests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ProviderPricingResult,
    guard_manifest_prune,
    reconcile_manifest_tombstones,
)


def write_embedding_provider_manifest(
    result: ProviderPricingResult,
    *,
    manifest_path: Path,
    required_model_ids: frozenset[str],
) -> list[str]:
    """Update input-only embedding prices in a provider manifest.

    Provider parsers use the normal ``ModelPrice`` contract, where embeddings
    carry a zero completion price. Keeping this writer shared prevents each
    embedding provider from inventing a subtly different manifest format.
    """
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError(f"{result.slug} manifest has no models list")

    updated: list[str] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("model_type") != "embedding":
            continue
        model_id = row.get("id")
        if not isinstance(model_id, str):
            continue
        price = result.prices.get(model_id)
        if price is None:
            continue
        if len(price.tiers) != 1 or price.completion_micro_per_m != 0:
            raise RuntimeError(
                f"{result.slug} embedding price for {model_id} must be "
                "single-tier and input-only"
            )
        row["input_token_price_per_m"] = price.prompt_micro_per_m
        row["output_token_price_per_m"] = 0
        row["pricing_source"] = result.fetched_url
        updated.append(model_id)

    missing = sorted(required_model_ids - set(updated))
    if missing:
        raise RuntimeError(
            f"{result.slug} manifest did not update required model(s): {missing}"
        )

    if result.fetched_url:
        raw["pricing_source"] = result.fetched_url
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(rows)
    manifest_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return [
        f"{result.slug}: refreshed provider_models/{manifest_path.name} "
        f"({len(updated)} priced rows)"
    ]


def write_discovered_chat_manifest(
    result: ProviderPricingResult,
    *,
    manifest_path: Path,
    discovered_rows: dict[str, dict[str, Any]],
    source_url: str,
) -> list[str]:
    """Rebuild a chat-provider manifest from a fresh provider catalog.

    Discovery modules own model normalization. This shared writer owns the
    safety behavior: preserve annotations, never publish an unpriced route,
    tombstone only after repeated fresh misses, and block mass pruning.
    """

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError(f"{result.slug} manifest has no models list")
    if not discovered_rows:
        guarded = guard_manifest_prune(rows, [], provider_slug=result.slug)
        if guarded is rows:
            return [f"{result.slug}: kept old manifest (mass-prune guard)"]
        raise RuntimeError(f"{result.slug} discovery returned no supported model rows")

    existing_by_id = {
        row["id"]: row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    present_rows: dict[str, dict[str, Any]] = {}
    updated: list[str] = []
    appended: list[str] = []
    for model_id, discovered in sorted(discovered_rows.items()):
        existing = existing_by_id.get(model_id)
        if existing is None:
            row: dict[str, Any] = {
                "display_name": str(discovered.get("display_name") or model_id),
                "title": str(discovered.get("upstream_id") or model_id),
                "model_type": "chat",
                "input_modalities": ["text"],
                "output_modalities": ["text"],
                "endpoints": ["chat/completions"],
                "status": 1,
            }
            appended.append(model_id)
        else:
            row = dict(existing)
        row.update(discovered)

        price = result.prices.get(model_id)
        if price is not None:
            tier = price.tiers[0]
            row["input_token_price_per_m"] = tier.prompt_micro_per_m
            row["output_token_price_per_m"] = tier.completion_micro_per_m
            if tier.prompt_cached_micro_per_m is None:
                row.pop("cached_input_token_price_per_m", None)
            else:
                row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
            updated.append(model_id)
        elif existing is None:
            row["routable"] = False
            row["routable_reason"] = "price-unavailable"
        present_rows[model_id] = row

    rebuilt = reconcile_manifest_tombstones(
        rows,
        present_rows,
        priced_ids=set(result.prices),
        source=result.source,
    )
    guarded = guard_manifest_prune(rows, rebuilt, provider_slug=result.slug)
    if guarded is rows:
        return [f"{result.slug}: kept old manifest (mass-prune guard)"]

    rebuilt_by_id = {
        row["id"]: row
        for row in rebuilt
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    tombstoned = sorted(
        model_id
        for model_id, old_row in existing_by_id.items()
        if old_row.get("routable") is not False
        and rebuilt_by_id.get(model_id, {}).get("routable") is False
    )
    raw["models"] = rebuilt
    raw["provider"] = result.slug
    raw["source"] = source_url
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["price_scale"] = "microdollars_per_million"
    raw["model_count"] = len(rebuilt)
    manifest_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    changes: list[str] = []
    if appended:
        changes.append(f"appended {len(appended)}")
    if tombstoned:
        changes.append(f"tombstoned {len(tombstoned)} unavailable")
    suffix = f", {', '.join(changes)}" if changes else ""
    return [
        f"{result.slug}: refreshed provider_models/{manifest_path.name} "
        f"({len(updated)} priced rows{suffix})"
    ]
