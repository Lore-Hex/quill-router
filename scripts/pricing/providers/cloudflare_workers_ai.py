"""Cloudflare Workers AI catalog and provider-native pricing refresh."""

from __future__ import annotations

import json
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
from scripts.pricing.manifest import apply_canary_results, write_discovered_chat_manifest
from scripts.pricing.model_ids import remember_upstream_id
from trusted_router.provider_contracts import (
    THIRD_PARTY_PASSTHROUGH_HOLD_REASON,
    provider_model_operator_held,
)

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
CLAIRIC_MODEL = "cloudflare/clairic"
CLAIRIC_UPSTREAM = "@cf/cloudflare/clairic"
CLAIRIC_PROBE = {
    "model": "clairic",
    "state": "Invoice INV-2291: total USD 1080.",
    "fields": {"invoice_number": {"type": "text"}, "total": {"type": "number"}},
}


def _clairic_row() -> dict[str, Any]:
    return {
        "display_name": "Cloudflare Clairic",
        "model_type": "chat",
        "endpoints": ["chat/completions"],
        # The adapter accepts images inside native task JSON, not OpenAI image
        # blocks. Do not advertise generic multimodal chat or token streaming.
        "input_modalities": ["text"],
        "output_modalities": ["text"],
        "supported_features": ["chat", "completion"],
        "supported_sampling_parameters": [],
        "documentation": {
            "description": "Preview typed extraction model: extract, compute, estimate or forecast values. Input-only billing, with confidence scores and distributions. Not a conversational model. Weights are not yet public.",
            "input_format": "POST /v1/chat/completions with cloudflare/clairic. Put native task JSON in exactly one user message: state, fields, and optionally reference_date, options and embedded images (PNG/JPEG/WebP, up to 4). Field types: text, number, date, time, datetime, duration, list, estimate, forecast. No tools, response_format or chat history. Native schema: https://developers.cloudflare.com/workers-ai/models/clairic/",
            "output_format": "JSON in choices[0].message.content: answers, confidence/probability metadata, truncated and native usage. Streaming delivers the completed extraction as one result. prompt_tokens is the provider's billable input_tokens across all passes; completion_tokens is zero. A null value means insufficient evidence. Check truncated before relying on the result.",
            "example_input": json.dumps({key: value for key, value in CLAIRIC_PROBE.items() if key != "model"}),
            "example_output": json.dumps({"answers": {"invoice_number": {"type": "text", "value": "INV-2291", "confidence": 0.99}, "total": {"type": "number", "value": 1080, "confidence": 0.99}}, "truncated": False}),
        },
    }


def _clairic_canary(client: httpx.Client, base_url: str) -> bool:
    try:
        response = client.post(
            base_url + "/ai/run/" + CLAIRIC_UPSTREAM,
            headers={
                "cf-aig-gateway-id": "default",
                "cf-aig-collect-log": "false",
                "cf-aig-collect-log-payload": "false",
                "cf-aig-skip-cache": "true",
            },
            json=CLAIRIC_PROBE,
        )
        response.raise_for_status()
        payload = response.json()
        result = payload["result"]
        usage = result["usage"]
        return (
            payload.get("success") is True
            and result.get("truncated") is False
            and result.get("model") in {"clairic", CLAIRIC_UPSTREAM}
            and result["answers"]["invoice_number"]["value"] == "INV-2291"
            and result["answers"]["total"]["value"] == 1080
            and type(usage.get("input_tokens")) is int
            and 0 < usage["input_tokens"] <= 1 << 31
            and type(usage.get("output_tokens")) is int
            and usage["output_tokens"] == 0
        )
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        return False

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
    if not amount.is_finite() or amount < 0:
        return None
    return int((amount * Decimal("1000000")).to_integral_value(ROUND_HALF_UP))


def _price_amount(item: dict[str, Any]) -> int | None:
    for key in ("price", "amount", "value"):
        if key in item:
            return _micro_per_m(item[key])
    return None


def _model_price(properties: dict[str, Any], *, input_only: bool = False) -> ModelPrice | None:
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
        if input_only and unit not in {"per m input tokens", "per m output tokens"}:
            return None
        if item.get("currency", "USD") != "USD":
            return None
        amount = _price_amount(item)
        if amount is None:
            continue
        if "cached" in unit and "input" in unit:
            cached = amount
        elif "input" in unit:
            if input_only and prompt is not None:
                return None
            prompt = amount
        elif "output" in unit:
            if input_only and completion is not None:
                return None
            completion = amount
    if input_only:
        # Only the reviewed Clairic adapter has this contract. Omission for an
        # ordinary chat model is NOT evidence that output is free.
        if completion not in (None, 0) or cached is not None or not prompt:
            return None
        completion = 0
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
    base_url = ACCOUNT_API.format(account_id=account_id)
    url = f"{base_url}/ai/models/search?per_page=100"
    transport = httpx.HTTPTransport(retries=PROVIDER_FETCH_TRANSPORT_RETRIES)
    with httpx.Client(
        timeout=PROVIDER_FETCH_TIMEOUT,
        follow_redirects=False,
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
        # A native extraction canary, never a chat PONG request. Price refresh
        # also catches loss of entitlement or a changed usage contract.
        rows = payload.get("result", []) if isinstance(payload, dict) else []
        clairic_present = isinstance(rows, list) and any(
            isinstance(row, dict) and row.get("name") == CLAIRIC_UPSTREAM for row in rows
        )
        clairic_healthy = (
            os.environ.get("TR_CLOUDFLARE_WORKERS_AI_ROUTABLE") == "1"
            and clairic_present and _clairic_canary(client, base_url)
        )

    source_rows = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(source_rows, list):
        source_rows = []
    account_enabled = os.environ.get("TR_CLOUDFLARE_WORKERS_AI_ROUTABLE") == "1"
    discovered = {model_id: dict(row) for model_id, row in _EARLY_ACCESS_MODELS.items()}
    prices = dict(_EARLY_ACCESS_PRICES)
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
        context = _positive_int(
            properties.get("context_window")
            or properties.get("context_length")
            or source.get("context_window")
        )
        if context is not None:
            row["context_length"] = context
        if model_id == CLAIRIC_MODEL:
            row.update(_clairic_row())
        discovered[model_id] = row
        price = _model_price(properties, input_only=model_id == CLAIRIC_MODEL)
        if price is not None:
            prices[model_id] = price

    for model_id, row in discovered.items():
        if account_enabled and model_id in prices:
            row["routable"] = True
            row.pop("routable_reason", None)
        else:
            row["routable"] = False
            row["routable_reason"] = (
                "awaiting-price" if account_enabled else "account-unfunded"
            )

    if account_enabled and CLAIRIC_MODEL in prices:
        apply_canary_results(
            discovered, checked_model_ids={CLAIRIC_MODEL},
            healthy_model_ids={CLAIRIC_MODEL} if clairic_healthy else set(),
        )

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
        operator_hold_reasons={
            model_id: THIRD_PARTY_PASSTHROUGH_HOLD_REASON
            for model_id, row in _DISCOVERED_MANIFEST_ROWS.items()
            if provider_model_operator_held(SLUG, model_id, str(row.get("upstream_id") or model_id))
        },
    )
