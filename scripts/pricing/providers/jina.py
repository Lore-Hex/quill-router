"""Jina authenticated embedding catalog and exact token prices."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    fetch_json,
    validate,
)
from scripts.pricing.manifest import write_discovered_embedding_manifest
from scripts.pricing.openai_catalog import dollars_per_token_to_micro_per_m, positive_int

SLUG = "jina"
BASE_URL = "https://api.jina.ai/v1"
URL = f"{BASE_URL}/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/jina.json"
)
MANIFEST_STALE_FALLBACK = True
INCLUDE_IN_PRICE_INDEX = False
EXPECTED_MODELS = (
    "jina-ai/jina-embeddings-v5-text-nano",
    "jina-ai/jina-embeddings-v5-text-small",
)

_discovered_rows: dict[str, dict[str, Any]] = {}


def _catalog_rows(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("jina: model catalog has no data list")
    return [row for row in payload["data"] if isinstance(row, dict)]


def _embedding_price(row: dict[str, Any]) -> ModelPrice | None:
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt = dollars_per_token_to_micro_per_m(pricing.get("prompt") or pricing.get("input"))
    if prompt is None or prompt <= 0:
        return None
    return ModelPrice(prompt, 0)


def _probe(api_key: str, upstream_model: str) -> bool:
    try:
        response = httpx.post(
            f"{BASE_URL}/embeddings",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": PROVIDER_FETCH_UA,
            },
            json={"model": upstream_model, "input": ["PONG"]},
            timeout=PROVIDER_FETCH_TIMEOUT,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200


def fetch() -> ProviderPricingResult:
    global _discovered_rows
    api_key = os.environ.get("JINA_API_KEY")
    if not api_key:
        raise RuntimeError("jina: JINA_API_KEY is required for discovery")
    rows = _catalog_rows(
        fetch_json(URL, extra_headers={"Authorization": f"Bearer {api_key}"})
    )
    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for source in rows:
        native_id = source.get("id")
        if not isinstance(native_id, str) or not native_id.startswith("jina-ai/"):
            continue
        output_modalities = {
            str(value).casefold() for value in (source.get("output_modalities") or [])
        }
        if "embeddings" not in output_modalities:
            continue
        price = _embedding_price(source)
        if price is None:
            continue
        upstream_id = native_id.removeprefix("jina-ai/")
        row: dict[str, Any] = {
            "id": native_id,
            "upstream_id": upstream_id,
            "display_name": str(source.get("name") or native_id),
            "input_modalities": [
                str(value) for value in (source.get("input_modalities") or ["text"])
            ],
        }
        context_length = positive_int(source.get("context_length"))
        if context_length is not None:
            row["context_length"] = context_length
        prices[native_id] = price
        discovered[native_id] = row

    errors = validate(prices, list(EXPECTED_MODELS))
    if errors:
        raise RuntimeError("; ".join(errors))
    representative = "jina-ai/jina-embeddings-v5-text-nano"
    healthy = _probe(api_key, discovered[representative]["upstream_id"])
    for row in discovered.values():
        row["routable"] = healthy
        if not healthy:
            row["routable_reason"] = "provider-canary-failed"
    _discovered_rows = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=[
            f"discovered {len(discovered)} priced embedding models",
            f"representative embedding canary {'passed' if healthy else 'failed'}",
        ],
        include_in_price_index=False,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _discovered_rows:
        raise RuntimeError("jina: fetch must succeed before writing manifest")
    return write_discovered_embedding_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_discovered_rows,
        source_url=URL,
    )
