"""Perplexity authenticated OpenAI-compatible native model catalog."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "perplexity"
BASE_URL = "https://api.perplexity.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/perplexity.json"


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Perplexity's USD/M catalog unit to the shared USD/token unit."""

    normalized: list[dict[str, Any]] = []
    for source in rows:
        pricing = source.get("pricing")
        if not isinstance(pricing, dict) or pricing.get("unit") != "usd_per_1m_tokens":
            continue
        try:
            prompt = Decimal(str(pricing["input"])) / Decimal(1_000_000)
            completion = Decimal(str(pricing["output"])) / Decimal(1_000_000)
            cached_raw = pricing.get("cache_read")
            cached = (
                Decimal(str(cached_raw)) / Decimal(1_000_000)
                if cached_raw is not None
                else None
            )
        except (InvalidOperation, KeyError, TypeError, ValueError):
            continue
        row = dict(source)
        normalized_pricing = {
            "prompt": str(prompt),
            "completion": str(completion),
        }
        if cached is not None:
            normalized_pricing["input_cache_read"] = str(cached)
        row["pricing"] = normalized_pricing
        normalized.append(row)
    return normalized


def _is_perplexity_route(row: dict[str, Any]) -> bool:
    model_id = row.get("id")
    if not isinstance(model_id, str) or not model_id.strip():
        return False
    return "/" not in model_id or model_id.startswith("perplexity/")


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="PERPLEXITY_API_KEY",
        explicit_model_map={},
        namespace_unqualified="perplexity",
        include=_is_perplexity_route,
        normalize_rows=_normalize_rows,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
