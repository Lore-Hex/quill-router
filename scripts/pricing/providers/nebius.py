"""Nebius Token Factory pricing refresh from the verbose models API."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
)

SLUG = "nebius"
URL = "https://api.tokenfactory.nebius.com/v1/models?verbose=true"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "nebius.json"
)

EXPECTED_MODELS = [
    "openai/gpt-oss-120b",
    "deepseek-ai/DeepSeek-V4-Pro",
    "zai-org/GLM-5.1",
]

_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}

_NATIVE_TO_CANONICAL = {
    # Nebius added this spelling after the same model was already published
    # under TrustedRouter's canonical cross-provider ID. Keep the native ID
    # only for upstream requests so refreshes cannot create a duplicate model.
    "nvidia/Nemotron-3-Ultra-550b-a55b": "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/NVIDIA-Nemotron-3-Ultra-550B-A55B": "nvidia/nemotron-3-ultra-550b-a55b",
}


def _dollars_per_token_to_micro_per_m(value: object) -> int | None:
    try:
        parsed = Decimal(str(value or "0"))
    except Exception:  # noqa: BLE001
        return None
    if parsed < 0:
        return None
    return int((parsed * Decimal(1_000_000_000_000)).to_integral_value())


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _manifest_row(
    row: dict[str, Any],
    price: ModelPrice,
    *,
    model_id: str,
    upstream_id: str,
) -> dict[str, Any]:
    architecture = row.get("architecture")
    architecture = architecture if isinstance(architecture, dict) else {}
    modality = str(architecture.get("modality") or "")
    input_modalities = ["text", "image"] if modality.startswith("text+image") else ["text"]
    out: dict[str, Any] = {
        "id": model_id,
        "upstream_id": upstream_id,
        "display_name": str(row.get("name") or model_id.rsplit("/", 1)[-1]),
        "title": model_id,
        "created": _positive_int(row.get("created")) or int(
            datetime.now(UTC).timestamp()
        ),
        "context_length": _positive_int(row.get("context_length")) or 131072,
        "max_output_tokens": 65536,
        "input_token_price_per_m": price.prompt_micro_per_m,
        "output_token_price_per_m": price.completion_micro_per_m,
        "model_type": "chat",
        "features": ["serverless"],
        "input_modalities": input_modalities,
        "output_modalities": ["text"],
        "endpoints": ["chat/completions"],
        "status": 1,
    }
    features = row.get("supported_features")
    if isinstance(features, list):
        normalized = {str(feature) for feature in features}
        if "tools" in normalized:
            out["features"].append("function-calling")
    return out


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS

    api_key = os.environ.get("NEBIUS_API_KEY") or os.environ.get(
        "NEBIUS_TOKEN_FACTORY_API_KEY"
    )
    headers = {"User-Agent": PROVIDER_FETCH_UA, "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
    ) as client:
        response = client.get(URL, headers=headers)
        response.raise_for_status()
        payload = response.json()

    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        rows = []
    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        upstream_id = row.get("id")
        if not isinstance(upstream_id, str):
            continue
        model_id = _NATIVE_TO_CANONICAL.get(upstream_id, upstream_id)
        architecture = row.get("architecture")
        architecture = architecture if isinstance(architecture, dict) else {}
        modality = str(architecture.get("modality") or "")
        if not modality.endswith("->text"):
            continue
        pricing = row.get("pricing")
        if not isinstance(pricing, dict):
            continue
        prompt = _dollars_per_token_to_micro_per_m(pricing.get("prompt"))
        completion = _dollars_per_token_to_micro_per_m(pricing.get("completion"))
        if prompt is None or completion is None:
            continue
        price = ModelPrice(prompt_micro_per_m=prompt, completion_micro_per_m=completion)
        prices[model_id] = price
        discovered[model_id] = _manifest_row(
            row,
            price,
            model_id=model_id,
            upstream_id=upstream_id,
        )

    _DISCOVERED_ROWS = discovered
    missing = sorted(set(EXPECTED_MODELS) - set(prices))
    if missing:
        raise RuntimeError(f"nebius missing expected model(s): {missing}")
    return ProviderPricingResult(slug=SLUG, prices=prices, source="api", fetched_url=URL)


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("nebius manifest has no models list")

    existing = {
        row["id"]: row
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    updated: list[str] = []
    appended: list[str] = []
    for model_id, price in sorted(result.prices.items()):
        row = existing.get(model_id)
        discovered = _DISCOVERED_ROWS.get(model_id)
        if row is None:
            if discovered is None:
                continue
            row = dict(discovered)
            rows.append(row)
            existing[model_id] = row
            appended.append(model_id)
        row["input_token_price_per_m"] = price.prompt_micro_per_m
        row["output_token_price_per_m"] = price.completion_micro_per_m
        updated.append(model_id)

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"nebius manifest did not update expected model(s): {missing}")

    raw["source"] = URL
    raw["generated_at"] = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    suffix = f", appended {len(appended)}" if appended else ""
    return [f"nebius: refreshed provider_models/nebius.json ({len(updated)} priced rows{suffix})"]
