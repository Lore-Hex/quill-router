"""Shared writers for committed provider-pricing manifests."""

from __future__ import annotations

import json
from collections.abc import Collection
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.pricing.base import (
    ProviderPricingResult,
    guard_manifest_prune,
    reconcile_manifest_tombstones,
)


def guard_fixed_output_prices(
    manifest_path: Path,
    discovered_rows: dict[str, dict[str, Any]],
) -> None:
    """Reject provider price changes that require an enclave release.

    Fixed media prices are enforced independently inside the enclave. The
    hourly catalog refresh may verify those prices, but it must never publish
    a new control-plane price before the matching enclave constants deploy.
    """

    if not manifest_path.exists():
        return
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError(f"{manifest_path.name} has no models list")
    old_by_id = {
        str(row.get("id")): row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    fixed_fields = (
        "fixed_output_price_microdollars",
        "fixed_output_price_per_second_microdollars",
    )
    changes: list[str] = []
    for model_id, discovered in discovered_rows.items():
        old = old_by_id.get(model_id)
        if old is None:
            continue
        for field in fixed_fields:
            if field not in discovered or field not in old:
                continue
            if discovered[field] != old[field]:
                changes.append(f"{model_id}.{field}: {old[field]!r} -> {discovered[field]!r}")
    if changes:
        raise RuntimeError(
            "fixed media price changed; update and deploy enclave billing first: "
            + "; ".join(changes)
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
                f"{result.slug} embedding price for {model_id} must be single-tier and input-only"
            )
        row["input_token_price_per_m"] = price.prompt_micro_per_m
        row["output_token_price_per_m"] = 0
        row["pricing_source"] = result.fetched_url
        updated.append(model_id)

    missing = sorted(required_model_ids - set(updated))
    if missing:
        raise RuntimeError(f"{result.slug} manifest did not update required model(s): {missing}")

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


def write_discovered_embedding_manifest(
    result: ProviderPricingResult,
    *,
    manifest_path: Path,
    discovered_rows: dict[str, dict[str, Any]],
    source_url: str,
) -> list[str]:
    """Rebuild a dynamic input-only embedding manifest through the shared writer."""

    normalized: dict[str, dict[str, Any]] = {}
    for model_id, source in discovered_rows.items():
        price = result.prices.get(model_id)
        if price is None:
            continue
        if len(price.tiers) != 1 or price.completion_micro_per_m != 0:
            raise RuntimeError(
                f"{result.slug} embedding price for {model_id} must be single-tier and input-only"
            )
        row = dict(source)
        row.update(
            {
                "id": model_id,
                "model_type": "embedding",
                "endpoints": ["embeddings"],
                "output_modalities": ["embeddings"],
            }
        )
        normalized[model_id] = row
    return write_discovered_chat_manifest(
        result,
        manifest_path=manifest_path,
        discovered_rows=normalized,
        source_url=source_url,
        pricing_source_url=source_url,
    )


def write_discovered_chat_manifest(
    result: ProviderPricingResult,
    *,
    manifest_path: Path,
    discovered_rows: dict[str, dict[str, Any]],
    source_url: str,
    pricing_source_url: str | None = None,
    operator_hold_reasons: dict[str, str] | None = None,
) -> list[str]:
    """Rebuild a chat-provider manifest from a fresh provider catalog.

    Discovery modules own model normalization. This shared writer owns the
    safety behavior: preserve annotations, never publish an unpriced route,
    tombstone only after repeated fresh misses, and block mass pruning.
    """

    if manifest_path.exists():
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    else:
        raw = {"provider": result.slug, "models": []}
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError(f"{result.slug} manifest has no models list")
    if not discovered_rows:
        guarded = guard_manifest_prune(rows, [], provider_slug=result.slug)
        if guarded is rows:
            return [f"{result.slug}: kept old manifest (mass-prune guard)"]
        raise RuntimeError(f"{result.slug} discovery returned no supported model rows")

    existing_by_id = {
        row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
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
        if discovered.get("routable") is True:
            # An explicit healthy signal from the provider adapter supersedes
            # machine-owned holds such as account-unfunded. Without removing
            # the stale reason, a funded account remains disabled forever even
            # after the adapter sets routable=true.
            row.pop("routable_reason", None)
            row.pop("unresolved_since", None)

        price = result.prices.get(model_id)
        if price is not None:
            if row.get("routable_reason") == "price-unavailable":
                row.pop("routable", None)
                row.pop("routable_reason", None)
            tier = price.tiers[0]
            row["input_token_price_per_m"] = tier.prompt_micro_per_m
            row["output_token_price_per_m"] = tier.completion_micro_per_m
            if tier.prompt_cached_micro_per_m is None:
                row.pop("cached_input_token_price_per_m", None)
            else:
                row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
            if len(price.tiers) == 1:
                row.pop("price_tiers", None)
            else:
                row["price_tiers"] = [
                    {
                        "max_prompt_tokens": price_tier.max_prompt_tokens,
                        "input_token_price_per_m": price_tier.prompt_micro_per_m,
                        "output_token_price_per_m": price_tier.completion_micro_per_m,
                        **(
                            {
                                "cached_input_token_price_per_m": (
                                    price_tier.prompt_cached_micro_per_m
                                )
                            }
                            if price_tier.prompt_cached_micro_per_m is not None
                            else {}
                        ),
                    }
                    for price_tier in price.tiers
                ]
            updated.append(model_id)
        else:
            row.pop("input_token_price_per_m", None)
            row.pop("output_token_price_per_m", None)
            row.pop("cached_input_token_price_per_m", None)
            row.pop("price_tiers", None)
            if (
                row.get("routable") is not False
                or row.get("routable_reason") == "price-unavailable"
            ):
                row["routable"] = False
                row["routable_reason"] = "price-unavailable"
        present_rows[model_id] = row

    rebuilt = reconcile_manifest_tombstones(
        rows,
        present_rows,
        priced_ids=set(result.prices),
        source=result.source,
    )
    # Discovery and tombstone recovery never have authority to clear an
    # operator safety hold. Apply these at the final manifest boundary so a
    # delist/relist cycle cannot accidentally publish a held route.
    _apply_operator_holds(rebuilt, operator_hold_reasons)
    guarded = guard_manifest_prune(rows, rebuilt, provider_slug=result.slug)
    operator_hold_only = False
    operator_hold_changes = 0
    if guarded is rows:
        # A prune guard protects availability, but must never veto a deliberate
        # safety hold. Apply only the holds to the old manifest and discard all
        # other discovered changes from this refresh.
        rebuilt = [dict(row) if isinstance(row, dict) else row for row in rows]
        operator_hold_changes = _apply_operator_holds(rebuilt, operator_hold_reasons)
        if operator_hold_changes == 0:
            return [f"{result.slug}: kept old manifest (mass-prune guard)"]
        operator_hold_only = True

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
    if pricing_source_url is not None:
        raw["pricing_source"] = pricing_source_url
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["price_scale"] = "microdollars_per_million"
    raw["model_count"] = len(rebuilt)
    manifest_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    if operator_hold_only:
        return [
            f"{result.slug}: applied {operator_hold_changes} operator hold(s); "
            "kept all other old rows (mass-prune guard)"
        ]

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


def _apply_operator_holds(
    rows: list[Any],
    operator_hold_reasons: dict[str, str] | None,
) -> int:
    changes = 0
    reasons = operator_hold_reasons or {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        reason = reasons.get(row.get("id"))
        if reason is None:
            continue
        if row.get("routable") is not False or row.get("routable_reason") != reason:
            changes += 1
        row["routable"] = False
        row["routable_reason"] = reason
    return changes


def models_requiring_canary(
    manifest_path: Path,
    discovered_model_ids: Collection[str],
    *,
    failure_reason: str = "provider-canary-failed",
) -> frozenset[str]:
    """Return only new routes and routes held by a previous failed canary."""

    if not manifest_path.exists():
        return frozenset(discovered_model_ids)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return frozenset(discovered_model_ids)
    rows = raw.get("models") if isinstance(raw, dict) else None
    if not isinstance(rows, list):
        return frozenset(discovered_model_ids)
    existing = {
        row["id"]: row for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    return frozenset(
        model_id
        for model_id in discovered_model_ids
        if model_id not in existing or existing[model_id].get("routable_reason") == failure_reason
    )


def apply_canary_results(
    discovered_rows: dict[str, dict[str, Any]],
    *,
    checked_model_ids: Collection[str],
    healthy_model_ids: Collection[str],
    failure_reason: str = "provider-canary-failed",
) -> None:
    """Attach machine-owned route state before the shared manifest rebuild."""

    checked = set(checked_model_ids)
    healthy = set(healthy_model_ids)
    if not healthy <= checked:
        raise ValueError("healthy model ids must be a subset of checked model ids")
    for model_id in checked:
        row = discovered_rows.get(model_id)
        if row is None:
            continue
        row["routable"] = model_id in healthy
        if model_id in healthy:
            row.pop("routable_reason", None)
        else:
            row["routable_reason"] = failure_reason


def set_manifest_canary_state(
    manifest_path: Path,
    *,
    healthy: bool,
    failure_reason: str = "provider-canary-failed",
) -> None:
    """Fail routes closed while preserving unrelated operator holds."""

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError(f"{manifest_path.name} has no models list")
    for row in rows:
        if not isinstance(row, dict):
            continue
        if healthy:
            if row.get("routable_reason") == failure_reason:
                row.pop("routable", None)
                row.pop("routable_reason", None)
        else:
            existing_reason = row.get("routable_reason")
            if row.get("routable") is False and existing_reason not in {
                None,
                failure_reason,
            }:
                continue
            row["routable"] = False
            row["routable_reason"] = failure_reason
    manifest_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def set_manifest_model_canary_states(
    manifest_path: Path,
    *,
    checked_model_ids: Collection[str],
    healthy_model_ids: Collection[str],
    failure_reason: str = "provider-canary-failed",
) -> None:
    """Apply authenticated canary state per model without disturbing holds."""

    checked = set(checked_model_ids)
    healthy = set(healthy_model_ids)
    if not healthy <= checked:
        raise ValueError("healthy model ids must be a subset of checked model ids")

    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError(f"{manifest_path.name} has no models list")
    for row in rows:
        if not isinstance(row, dict) or row.get("id") not in checked:
            continue
        if row["id"] in healthy:
            if row.get("routable_reason") == failure_reason:
                row.pop("routable", None)
                row.pop("routable_reason", None)
            continue
        existing_reason = row.get("routable_reason")
        if row.get("routable") is False and existing_reason not in {
            None,
            failure_reason,
        }:
            continue
        row["routable"] = False
        row["routable_reason"] = failure_reason
    manifest_path.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
