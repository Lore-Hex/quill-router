"""Shared parsing for provider-owned OpenAI-compatible model catalogs."""

from __future__ import annotations

from collections.abc import Callable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

import httpx

from scripts.pricing.base import PROVIDER_FETCH_TIMEOUT, PROVIDER_FETCH_UA, ModelPrice
from scripts.pricing.model_ids import mapped_or_canonical_model_id, remember_upstream_id


def dollars_per_token_to_micro_per_m(value: object) -> int | None:
    """Convert provider dollars/token strings to integer microdollars/M."""

    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return int((parsed * Decimal("1000000000000")).to_integral_value(ROUND_HALF_UP))


def positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def openai_model_price(row: dict[str, Any]) -> ModelPrice | None:
    pricing = row.get("pricing")
    if not isinstance(pricing, dict):
        return None
    prompt = dollars_per_token_to_micro_per_m(
        pricing.get("prompt") or pricing.get("input")
    )
    completion = dollars_per_token_to_micro_per_m(
        pricing.get("completion") or pricing.get("output")
    )
    if prompt is None or completion is None:
        return None
    cached = dollars_per_token_to_micro_per_m(
        pricing.get("input_cache_read")
        or pricing.get("input_cache_reads")
        or pricing.get("cache_read")
    )
    return ModelPrice(
        prompt_micro_per_m=prompt,
        completion_micro_per_m=completion,
        prompt_cached_micro_per_m=cached,
    )


def discover_openai_chat_catalog(
    rows: list[dict[str, Any]],
    *,
    explicit_map: dict[str, str],
    upstream_id_map: dict[str, str],
    include: Callable[[dict[str, Any]], bool] | None = None,
) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    """Normalize priced text-chat rows while preserving exact upstream IDs."""

    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    for source in rows:
        native_id = source.get("id")
        if not isinstance(native_id, str) or not native_id.strip():
            continue
        if include is not None and not include(source):
            continue
        output_modalities = source.get("output_modalities")
        if isinstance(output_modalities, list) and output_modalities:
            if "text" not in {str(item).casefold() for item in output_modalities}:
                continue
        model_id = mapped_or_canonical_model_id(native_id, explicit_map)
        if model_id is None:
            continue
        price = openai_model_price(source)
        if price is None:
            continue

        remember_upstream_id(upstream_id_map, model_id, native_id)
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": str(source.get("name") or source.get("description") or native_id),
            "endpoints": ["chat/completions"],
        }
        context_length = positive_int(source.get("context_length"))
        if context_length is not None:
            row["context_length"] = context_length
        max_output = positive_int(
            source.get("max_output_length") or source.get("max_output_tokens")
        )
        if max_output is not None:
            row["max_output_tokens"] = max_output
        for field in (
            "input_modalities",
            "output_modalities",
            "supported_features",
            "supported_sampling_parameters",
        ):
            value = source.get(field)
            if isinstance(value, list):
                row[field] = [str(item) for item in value]
        discovered[model_id] = row
        prices[model_id] = price
    return prices, discovered


def probe_openai_chat(*, base_url: str, api_key: str | None, model: str) -> bool:
    """Run a minimal paid-path canary without logging response content."""

    if not api_key:
        return False
    try:
        response = httpx.post(
            f"{base_url.rstrip('/')}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": PROVIDER_FETCH_UA,
            },
            json={
                "model": model,
                "messages": [{"role": "user", "content": "Reply PONG"}],
                "max_tokens": 4,
                "stream": False,
            },
            timeout=PROVIDER_FETCH_TIMEOUT,
        )
    except httpx.HTTPError:
        return False
    return response.status_code == 200
