"""Wafer — provider-native model catalog and pricing.

Wafer exposes an OpenAI-compatible API at https://pass.wafer.ai/v1. Its
`/models` response is the source of truth for model availability, ZDR support,
capabilities, and prices. Prices are published as cents per million tokens,
so this module converts directly to integer microdollars per million tokens.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx

from scripts.pricing.base import (
    PROVIDER_FETCH_TIMEOUT,
    PROVIDER_FETCH_TRANSPORT_RETRIES,
    PROVIDER_FETCH_UA,
    ModelPrice,
    ProviderPricingResult,
    validate,
)
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id

SLUG = "wafer"
URL = "https://pass.wafer.ai/v1/models"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "wafer.json"
)

EXPECTED_MODELS = [
    "z-ai/glm-5.1",
    "z-ai/glm-5.2",
    "z-ai/glm-5.2-fast",
    "moonshotai/kimi-k2.6",
    "minimax/minimax-m3",
]

_NATIVE_TO_OR_ID = {
    "GLM-5.1": "z-ai/glm-5.1",
    "GLM-5.2": "z-ai/glm-5.2",
    "GLM-5.2-Fast": "z-ai/glm-5.2-fast",
    "glm5.2-fast": "z-ai/glm-5.2-fast",
    "Kimi-K2.6": "moonshotai/kimi-k2.6",
    "Kimi-K2.7-Code": "moonshotai/kimi-k2.7-code",
    "Qwen3.5-397B-A17B": "qwen/qwen3.5-397b-a17b",
    "Qwen3.6-35B-A3B": "qwen/qwen3.6-35b-a3b",
    "qwen3.6-max-preview": "qwen/qwen3.6-max-preview",
    "qwen3.7-max": "qwen/qwen3.7-max",
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "MiniMax-M3": "minimax/minimax-m3",
}

UPSTREAM_ID_MAP = {or_id: native_id for native_id, or_id in _NATIVE_TO_OR_ID.items()}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _cents_to_micro_per_m(value: object) -> int | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return int((parsed * Decimal("10000")).to_integral_value(ROUND_HALF_UP))


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _wafer_capabilities(row: dict[str, Any]) -> dict[str, Any]:
    wafer = row.get("wafer")
    if not isinstance(wafer, dict):
        return {}
    capabilities = wafer.get("capabilities")
    return capabilities if isinstance(capabilities, dict) else {}


def _wafer_pricing(row: dict[str, Any]) -> dict[str, Any] | None:
    wafer = row.get("wafer")
    if not isinstance(wafer, dict):
        return None
    pricing = wafer.get("pricing")
    return pricing if isinstance(pricing, dict) else None


def _manifest_row(
    *,
    model_id: str,
    native_id: str,
    source_row: dict[str, Any],
    price: ModelPrice,
) -> dict[str, Any]:
    wafer = source_row.get("wafer")
    wafer = wafer if isinstance(wafer, dict) else {}
    capabilities = _wafer_capabilities(source_row)
    chat_capabilities = capabilities.get("chat_completions")
    chat_capabilities = chat_capabilities if isinstance(chat_capabilities, dict) else {}
    message_capabilities = capabilities.get("messages")
    message_capabilities = (
        message_capabilities if isinstance(message_capabilities, dict) else {}
    )
    zdr_capabilities = capabilities.get("zdr")
    zdr_capabilities = zdr_capabilities if isinstance(zdr_capabilities, dict) else {}
    supports_vision = bool(
        capabilities.get("vision")
        or chat_capabilities.get("vision")
        or message_capabilities.get("vision")
    )

    row: dict[str, Any] = {
        "id": model_id,
        "upstream_id": native_id,
        "display_name": str(wafer.get("display_name") or native_id),
        "endpoints": ["chat/completions"],
        "input_token_price_per_m": price.prompt_micro_per_m,
        "output_token_price_per_m": price.completion_micro_per_m,
        "zdr_supported": bool(zdr_capabilities.get("supported")),
    }
    context_length = _positive_int(wafer.get("context_length"))
    if context_length is not None:
        row["context_length"] = context_length
    if supports_vision:
        row["input_modalities"] = ["text", "image"]
        row["output_modalities"] = ["text"]
    tier = price.tiers[0]
    if tier.prompt_cached_micro_per_m is not None:
        row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
    return row


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS

    api_key = os.environ.get("WAFER_API_KEY")
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
    manifest_rows: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        native_id = row.get("id")
        if not isinstance(native_id, str):
            continue
        or_id = mapped_or_canonical_model_id(native_id, _NATIVE_TO_OR_ID)
        if or_id is None:
            continue
        remember_upstream_id(UPSTREAM_ID_MAP, or_id, native_id)
        pricing = _wafer_pricing(row)
        if not isinstance(pricing, dict):
            continue
        prompt = _cents_to_micro_per_m(pricing.get("input_cents_per_million"))
        completion = _cents_to_micro_per_m(pricing.get("output_cents_per_million"))
        if prompt is None or completion is None:
            continue
        cache_read = _cents_to_micro_per_m(pricing.get("cache_read_cents_per_million"))
        price = ModelPrice(
            prompt_micro_per_m=prompt,
            completion_micro_per_m=completion,
            prompt_cached_micro_per_m=cache_read,
        )
        prices[or_id] = price
        manifest_rows[or_id] = _manifest_row(
            model_id=or_id,
            native_id=native_id,
            source_row=row,
            price=price,
        )

    _DISCOVERED_MANIFEST_ROWS = manifest_rows

    notes: list[str] = []
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        notes.append(f"validation notes: {errors}")
        raise RuntimeError("; ".join(errors))

    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=URL,
        notes=notes,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    """Update Wafer's provider-native runtime manifest from `/v1/models`.

    Wafer models are supplemental provider routes, so hourly price refreshes
    must update `provider_models/wafer.json`, not only the OR-shaped snapshot.
    This also lets newly launched Wafer-native IDs become routable as soon as
    the Wafer API publishes the model + pricing.
    """

    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    rows = raw.get("models")
    if not isinstance(rows, list):
        raise RuntimeError("wafer manifest has no models list")

    previous_ids = {
        row["id"] for row in rows if isinstance(row, dict) and isinstance(row.get("id"), str)
    }
    updated: list[str] = []
    refreshed_rows: list[dict[str, Any]] = []
    for model_id, price in sorted(result.prices.items()):
        discovered = _DISCOVERED_MANIFEST_ROWS.get(model_id)
        if discovered is None:
            continue
        row = dict(discovered)

        tier = price.tiers[0]
        row["input_token_price_per_m"] = tier.prompt_micro_per_m
        row["output_token_price_per_m"] = tier.completion_micro_per_m
        if tier.prompt_cached_micro_per_m is not None:
            row["cached_input_token_price_per_m"] = tier.prompt_cached_micro_per_m
        else:
            row.pop("cached_input_token_price_per_m", None)
        refreshed_rows.append(row)
        updated.append(model_id)

    missing = sorted(set(EXPECTED_MODELS) - set(updated))
    if missing:
        raise RuntimeError(f"wafer manifest did not update expected model(s): {missing}")
    if not updated:
        raise RuntimeError("wafer manifest update touched no rows")

    # Wafer's authenticated /v1/models feed is both the availability and
    # pricing source. Rebuild the provider supplement from that feed so
    # retired models cannot remain routable with stale prices.
    rows[:] = refreshed_rows
    current_ids = set(updated)
    appended = sorted(current_ids - previous_ids)
    removed = sorted(previous_ids - current_ids)

    raw["_about"] = (
        "Provider-native supplement for Wafer serverless API. Refreshed "
        "hourly from Wafer's OpenAI-compatible /v1/models feed."
    )
    raw["source"] = URL
    raw["generated_at"] = (
        datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    raw["model_count"] = len(rows)
    MANIFEST_PATH.write_text(
        json.dumps(raw, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    changes: list[str] = []
    if appended:
        changes.append(f"appended {len(appended)}")
    if removed:
        changes.append(f"removed {len(removed)} unavailable")
    suffix = f", {', '.join(changes)}" if changes else ""
    return [f"wafer: refreshed provider_models/wafer.json ({len(updated)} priced rows{suffix})"]
