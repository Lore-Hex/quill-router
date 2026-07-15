"""Shared writers for committed provider-pricing manifests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from scripts.pricing.base import ProviderPricingResult


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
