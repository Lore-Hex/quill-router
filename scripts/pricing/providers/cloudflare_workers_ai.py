"""Cloudflare Workers AI catalog and provider-native pricing refresh."""

from __future__ import annotations

import os
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
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.model_ids import remember_upstream_id

SLUG = "cloudflare-workers-ai"
ACCOUNT_API = "https://api.cloudflare.com/client/v4/accounts/{account_id}"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "cloudflare-workers-ai.json"
)
EXPECTED_MODELS = ["moonshotai/kimi-k2.7-code"]

# Kimi K3 was published in the Workers AI dashboard before it appeared in the
# account model-search feed. Keep this narrow bridge until discovery returns it.
_EARLY_ACCESS_MODELS: dict[str, dict[str, Any]] = {
    "moonshotai/kimi-k3": {
        "id": "moonshotai/kimi-k3",
        "upstream_id": "moonshotai/kimi-k3",
        "display_name": "Kimi K3",
        "context_length": 1_048_576,
        "input_modalities": ["text", "image"],
        "output_modalities": ["text"],
        "supported_features": ["reasoning", "streaming", "tools", "vision"],
    }
}
_EARLY_ACCESS_PRICES = {
    "moonshotai/kimi-k3": ModelPrice(
        prompt_micro_per_m=3_000_000,
        completion_micro_per_m=15_000_000,
        prompt_cached_micro_per_m=300_000,
    )
}

UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _canonical_model_id(native_id: str) -> str:
    model_id = native_id.removeprefix("@cf/")
    replacements = {
        "deepseek-ai/": "deepseek/",
        "meta/": "meta-llama/",
        "mistral/": "mistralai/",
        "zai-org/": "z-ai/",
    }
    for source, target in replacements.items():
        if model_id.startswith(source):
            return target + model_id.removeprefix(source).lower()
    author, separator, slug = model_id.partition("/")
    return f"{author.lower()}{separator}{slug.lower()}"


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _properties(source: dict[str, Any]) -> dict[str, Any]:
    raw = source.get("properties")
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, list):
        return {}
    result: dict[str, Any] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("property_id") or item.get("name") or item.get("id")
        if isinstance(name, str):
            result[name.lower()] = item.get("value")
    return result


def _micro_per_m(value: object) -> int | None:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if amount < 0:
        return None
    return int((amount * Decimal("1000000")).to_integral_value(ROUND_HALF_UP))


def _price_amount(item: dict[str, Any]) -> int | None:
    for key in ("price", "amount", "value"):
        if key in item:
            return _micro_per_m(item[key])
    return None


def _model_price(properties: dict[str, Any]) -> ModelPrice | None:
    raw = properties.get("price") or properties.get("pricing")
    if isinstance(raw, dict):
        raw = raw.get("items") or raw.get("prices") or raw.get("value")
    if not isinstance(raw, list):
        return None
    prompt: int | None = None
    cached: int | None = None
    completion: int | None = None
    for item in raw:
        if not isinstance(item, dict):
            continue
        unit = str(item.get("unit") or item.get("name") or "").lower()
        amount = _price_amount(item)
        if amount is None:
            continue
        if "cached" in unit and "input" in unit:
            cached = amount
        elif "input" in unit:
            prompt = amount
        elif "output" in unit:
            completion = amount
    if prompt is None or completion is None:
        return None
    return ModelPrice(
        prompt_micro_per_m=prompt,
        completion_micro_per_m=completion,
        prompt_cached_micro_per_m=cached,
    )


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    token = os.environ.get("CLOUDFLARE_WORKERS_AI_API_TOKEN")
    account_id = os.environ.get("CLOUDFLARE_WORKERS_AI_ACCOUNT_ID")
    if not token or not account_id:
        raise RuntimeError(
            "CLOUDFLARE_WORKERS_AI_API_TOKEN and "
            "CLOUDFLARE_WORKERS_AI_ACCOUNT_ID are required"
        )
    url = f"{ACCOUNT_API.format(account_id=account_id)}/ai/models/search?per_page=100"
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=True,
        transport=transport,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": PROVIDER_FETCH_UA,
        },
    ) as client:
        response = client.get(url)
        response.raise_for_status()
        payload = response.json()

    source_rows = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        source_rows = []
    account_enabled = os.environ.get("TR_CLOUDFLARE_WORKERS_AI_ROUTABLE") == "1"
    discovered = {model_id: dict(row) for model_id, row in _EARLY_ACCESS_MODELS.items()}
    prices = dict(_EARLY_ACCESS_PRICES)
    if not account_enabled:
        for row in discovered.values():
            row["routable"] = False
            row["routable_reason"] = "account-unfunded"
    for source in source_rows:
        if not isinstance(source, dict):
            continue
        task = source.get("task")
        task_name = task.get("name") if isinstance(task, dict) else task
        if not isinstance(task_name, str) or task_name.lower() != "text generation":
            continue
        # Cloudflare's account model-search response uses an opaque UUID in
        # `id`; `name` is the model identifier accepted by the OpenAI surface.
        native_id = source.get("name")
        if not isinstance(native_id, str) or not native_id:
            continue
        model_id = _canonical_model_id(native_id)
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, native_id)
        properties = _properties(source)
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": str(source.get("name") or model_id),
        }
        if not account_enabled:
            row["routable"] = False
            row["routable_reason"] = "account-unfunded"
        context = _positive_int(
            properties.get("context_window")
            or properties.get("context_length")
            or source.get("context_window")
        )
        if context is not None:
            row["context_length"] = context
        discovered[model_id] = row
        price = _model_price(properties)
        if price is not None:
            prices[model_id] = price

    for model_id, row in _EARLY_ACCESS_MODELS.items():
        remember_upstream_id(UPSTREAM_ID_MAP, model_id, str(row["upstream_id"]))
    _DISCOVERED_MANIFEST_ROWS = discovered
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=url,
        notes=[f"discovered {len(discovered)} Workers AI text-generation models"],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=result.fetched_url or "https://developers.cloudflare.com/workers-ai/",
    )
