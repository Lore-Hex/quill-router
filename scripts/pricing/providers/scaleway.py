"""Scaleway Generative APIs catalog and exact EUR pricing refresh.

Scaleway exposes model availability through its authenticated OpenAI-compatible
catalog and embeds machine-readable prices and capabilities in its official
pricing page.  A model is admitted only when both sources agree.  EUR prices
are converted with the ECB's current EUR/USD reference rate using Decimal math
and rounded up to one microdollar per million tokens.
"""

from __future__ import annotations

import json
import os
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from defusedxml import ElementTree as ET

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_html,
    fetch_json,
    validate,
)
from scripts.pricing.manifest import write_discovered_chat_manifest
from scripts.pricing.openai_catalog import positive_int

SLUG = "scaleway"
BASE_URL = "https://api.scaleway.ai/v1"
MODELS_URL = f"{BASE_URL}/models"
PRICING_URL = "https://www.scaleway.com/en/pricing/model-as-a-service/"
ECB_RATES_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
URL = PRICING_URL
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "scaleway.json"
)

_PRICING_MARKER = '"generativeApis":{"models":'
_NATIVE_TO_CANONICAL = {
    "llama-3.3-70b-instruct": "meta-llama/llama-3.3-70b-instruct",
    "pixtral-12b-2409": "mistralai/pixtral-12b-2409",
    "bge-multilingual-gemma2": "baai/bge-multilingual-gemma2",
    "qwen3-235b-a22b-instruct-2507": "qwen/qwen3-235b-a22b-instruct-2507",
    "mistral-small-3.2-24b-instruct-2506": (
        "mistralai/mistral-small-3.2-24b-instruct-2506"
    ),
    "qwen3-coder-30b-a3b-instruct": "qwen/qwen3-coder-30b-a3b-instruct",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "qwen3-embedding-8b": "qwen/qwen3-embedding-8b",
    "holo2-30b-a3b": "hcompany/holo2-30b-a3b",
    "qwen3.5-397b-a17b": "qwen/qwen3.5-397b-a17b",
    "gemma-4-26b-a4b-it": "google/gemma-4-26b-a4b-it",
    "qwen3.6-35b-a3b": "qwen/qwen3.6-35b-a3b",
    "mistral-medium-3.5-128b": "mistralai/mistral-medium-3.5-128b",
    "glm-5.2": "z-ai/glm-5.2",
}
_PROVIDER_NAMESPACES = {
    "baai": "baai",
    "google": "google",
    "hcompany": "hcompany",
    "meta": "meta-llama",
    "mistral": "mistralai",
    "openai": "openai",
    "qwen": "qwen",
    "zai": "z-ai",
}
_EMBEDDING_CONTEXTS = {
    "bge-multilingual-gemma2": 8_192,
    "qwen3-embedding-8b": 32_768,
}
EXPECTED_MODELS = ["z-ai/glm-5.2", "openai/gpt-oss-120b"]
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_MANIFEST_ROWS: dict[str, dict[str, Any]] = {}


def _extract_pricing_models(html: str) -> list[dict[str, Any]]:
    """Decode Scaleway's structured pricing payload without scraping text."""

    marker_index = html.find(_PRICING_MARKER)
    if marker_index < 0:
        raise RuntimeError("scaleway: structured Generative APIs prices are missing")
    payload_start = marker_index + len(_PRICING_MARKER)
    try:
        payload, _end = json.JSONDecoder().raw_decode(html[payload_start:])
    except json.JSONDecodeError as exc:
        raise RuntimeError("scaleway: invalid structured Generative APIs prices") from exc
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("scaleway: structured Generative APIs model list is empty")
    rows = [row for row in payload if isinstance(row, dict)]
    if len(rows) != len(payload):
        raise RuntimeError("scaleway: structured Generative APIs model row is invalid")
    return rows


def _parse_eur_usd(xml: str) -> Decimal:
    """Read the current official ECB USD value for one EUR."""

    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise RuntimeError("scaleway: invalid ECB reference-rate XML") from exc
    for element in root.iter():
        if element.attrib.get("currency") != "USD":
            continue
        try:
            rate = Decimal(element.attrib["rate"])
        except (InvalidOperation, KeyError) as exc:
            raise RuntimeError("scaleway: invalid ECB EUR/USD reference rate") from exc
        if not rate.is_finite() or rate <= 0:
            raise RuntimeError("scaleway: invalid ECB EUR/USD reference rate")
        return rate
    raise RuntimeError("scaleway: ECB EUR/USD reference rate is missing")


def _money_eur(value: object) -> Decimal:
    if not isinstance(value, dict) or value.get("currencyCode") != "EUR":
        raise RuntimeError("scaleway: token price is not an exact EUR amount")
    try:
        units = Decimal(int(value.get("units", 0)))
        nanos = Decimal(int(value.get("nanos", 0))) / Decimal(1_000_000_000)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("scaleway: token price has invalid units") from exc
    amount = units + nanos
    if not amount.is_finite() or amount < 0:
        raise RuntimeError("scaleway: token price must be finite and non-negative")
    return amount


def _usd_micro_per_m(eur_per_m: Decimal, eur_usd: Decimal) -> int:
    return int(
        (eur_per_m * eur_usd * Decimal(1_000_000)).to_integral_value(
            rounding=ROUND_CEILING
        )
    )


def _canonical_model_id(native_id: str, provider_name: object) -> str | None:
    explicit = _NATIVE_TO_CANONICAL.get(native_id)
    if explicit is not None:
        return explicit
    namespace = _PROVIDER_NAMESPACES.get(str(provider_name or "").strip().casefold())
    normalized = native_id.strip().casefold()
    if namespace is None or not normalized or "/" in normalized:
        return None
    return f"{namespace}/{normalized}"


def _token_price(region: dict[str, Any], field: str, eur_usd: Decimal) -> int:
    price = region.get(field)
    per_million = price.get("perMillionTokens") if isinstance(price, dict) else None
    if not isinstance(per_million, dict) or per_million.get("isApproximation") is True:
        raise RuntimeError(f"scaleway: {field} has no exact per-million-token price")
    return _usd_micro_per_m(_money_eur(per_million.get("value")), eur_usd)


def _live_model_ids(payload: object) -> set[str]:
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("scaleway: authenticated /models response has no data list")
    model_ids = {
        str(row["id"])
        for row in rows
        if isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
    }
    if not model_ids:
        raise RuntimeError("scaleway: authenticated /models response is empty")
    return model_ids


def _catalog(
    live_payload: object,
    pricing_html: str,
    eur_usd: Decimal,
) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]], int]:
    live_ids = _live_model_ids(live_payload)
    pricing_rows = {
        str(row["apiId"]): row
        for row in _extract_pricing_models(pricing_html)
        if isinstance(row.get("apiId"), str)
    }
    missing_prices = sorted(live_ids - set(pricing_rows))
    if missing_prices:
        raise RuntimeError(
            f"scaleway: live models missing structured prices: {missing_prices}"
        )

    prices: dict[str, ModelPrice] = {}
    discovered: dict[str, dict[str, Any]] = {}
    unsupported_count = 0
    for native_id in sorted(live_ids):
        source = pricing_rows[native_id]
        supported_apis = {
            str(item).removeprefix("/v1/")
            for item in source.get("supportedApis", [])
            if isinstance(item, str)
        }
        if "chat/completions" in supported_apis:
            model_type = "chat"
            endpoints = ["chat/completions"]
        elif "embeddings" in supported_apis:
            model_type = "embedding"
            endpoints = ["embeddings"]
        else:
            unsupported_count += 1
            continue

        model_id = _canonical_model_id(native_id, source.get("providerName"))
        if model_id is None:
            raise RuntimeError(
                f"scaleway: cannot normalize live model {native_id!r} from "
                f"provider {source.get('providerName')!r}"
            )
        UPSTREAM_ID_MAP[model_id] = native_id
        regions = source.get("regions")
        if not isinstance(regions, list):
            raise RuntimeError(f"scaleway: {native_id} has no region prices")
        region_rows = [row for row in regions if isinstance(row, dict)]
        paris = next((row for row in region_rows if row.get("region") == "fr-par"), None)
        if paris is None:
            raise RuntimeError(f"scaleway: {native_id} has no fr-par price")

        input_price = _token_price(paris, "inputTokenPrice", eur_usd)
        output_price = (
            _token_price(paris, "outputTokenPrice", eur_usd)
            if model_type == "chat"
            else 0
        )
        prices[model_id] = ModelPrice(
            prompt_micro_per_m=input_price,
            completion_micro_per_m=output_price,
        )

        tasks = {
            str(task).casefold()
            for task in source.get("tasks", [])
            if isinstance(task, str)
        }
        input_modalities = ["text"]
        if "vision" in tasks:
            input_modalities.append("image")
        supported_features: list[str] = []
        if source.get("toolCallingSupported") is True:
            supported_features.append("tools")
        if source.get("reasoning") is True:
            supported_features.append("reasoning")

        context_length = positive_int(source.get("contextWindow"))
        if context_length is None:
            context_length = _EMBEDDING_CONTEXTS.get(native_id)
        if context_length is None:
            raise RuntimeError(f"scaleway: {native_id} has no context length")
        row: dict[str, Any] = {
            "id": model_id,
            "upstream_id": native_id,
            "display_name": str(source.get("name") or native_id),
            "title": native_id,
            "model_type": model_type,
            "context_length": context_length,
            "input_modalities": input_modalities,
            "output_modalities": ["text"],
            "endpoints": endpoints,
            "provider_regions": [
                str(region["region"])
                for region in region_rows
                if isinstance(region.get("region"), str)
            ],
            "supported_features": supported_features,
            "status": 1,
        }
        max_output_tokens = positive_int(source.get("maxOutputTokens"))
        if max_output_tokens is not None:
            row["max_output_tokens"] = max_output_tokens
        discovered[model_id] = row

    if not discovered:
        raise RuntimeError("scaleway: no supported chat or embedding models")
    return prices, discovered, unsupported_count


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_MANIFEST_ROWS  # noqa: PLW0603

    api_key = os.environ.get("SCALEWAY_SECRET_KEY")
    if not api_key:
        raise RuntimeError("scaleway: SCALEWAY_SECRET_KEY is required")
    headers = {"Authorization": f"Bearer {api_key}"}
    live_payload = fetch_json(MODELS_URL, extra_headers=headers)
    pricing_html = fetch_html(PRICING_URL)
    eur_usd = _parse_eur_usd(fetch_html(ECB_RATES_URL))
    prices, discovered, unsupported_count = _catalog(
        live_payload,
        pricing_html,
        eur_usd,
    )
    errors = validate(prices, EXPECTED_MODELS)
    if errors:
        raise RuntimeError("; ".join(errors))
    _DISCOVERED_MANIFEST_ROWS = discovered
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=PRICING_URL,
        notes=[
            f"discovered {len(discovered)} priced chat/embedding models",
            f"left {unsupported_count} unsupported audio model(s) unlisted",
            f"converted exact EUR prices at ECB EUR/USD {eur_usd}",
        ],
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_MANIFEST_ROWS,
        source_url=MODELS_URL,
    )
