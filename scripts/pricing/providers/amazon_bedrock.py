"""Bedrock Mantle discovery joined to exact regional, standard-tier AWS SKUs.

Never substitute Runtime, global, priority, batch, or a similarly named model's
price. Availability does not imply account entitlement: new routes need a canary.
"""

from __future__ import annotations

import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_html, fetch_json, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import mapped_or_canonical_model_id
from scripts.pricing.openai_catalog import probe_openai_chat

SLUG = "amazon-bedrock"
REGION = "us-east-1"
BASE_URL = f"https://bedrock-mantle.{REGION}.api.aws/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = f"https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonBedrock/current/{REGION}/index.json"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/amazon-bedrock.json"
MANIFEST_STALE_FALLBACK = True
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}
_SKU = re.compile(r"USE1-(.+)-mantle-(input|output|cache-read)-tokens-standard")
_MODEL_MAP = {
    "google.gemma-4-31b": "google/gemma-4-31b-it",
    "nvidia.nemotron-nano-3-30b": "nvidia/nemotron-3-nano-30b-a3b",
    "nvidia.nemotron-super-3-120b": "nvidia/nemotron-3-super-120b-a12b",
    "qwen.qwen3-235b-a22b-2507": "qwen/qwen3-235b-a22b-instruct-2507",
    "mistral.mistral-large-3-675b-instruct": "mistralai/mistral-large-2512",
}


def model_card_limits(html: str, native: str) -> dict[str, int]:
    soup = BeautifulSoup(html, "html.parser")
    # A redirected/general documentation page must never supply another
    # model's limits. Require this exact ID in the Mantle programmatic row.
    if not any(
        "bedrock-mantle" in [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        and native in [cell.get_text(" ", strip=True) for cell in row.find_all("td")]
        for row in soup.select("tr")
    ):
        return {}
    fields = {"Context window": "context_length", "Max output tokens": "max_output_tokens"}
    values: dict[str, int] = {}
    for item in soup.select("li"):
        text = item.get_text(" ", strip=True)
        match = re.fullmatch(r"(Context window|Max output tokens):\s*([0-9,]+(?:\.[0-9]+)?)\s*([KM]?)(?:\s+tokens)?", text)
        if match:
            field = fields[match[1]]
            value = int(Decimal(match[2].replace(",", "")) * {"": 1, "K": 1000, "M": 1_000_000}[match[3]])
            if field in values and values[field] != value:
                return {}
            values[field] = value
    if not 0 < values.get("max_output_tokens", 0) <= values.get("context_length", 0) <= 10_000_000:
        return {}
    return values


def fetch_model_limits(native: str) -> dict[str, int]:
    url = "https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-" + native.replace(".", "-") + ".html"
    try:
        return model_card_limits(fetch_html(url), native)
    except (httpx.HTTPError, RuntimeError):
        return {}


def canonical_model_id(native: str) -> str | None:
    if native in _MODEL_MAP:
        return _MODEL_MAP[native]
    if not re.fullmatch(r"[a-z0-9]+\.[a-z0-9][a-z0-9.-]*", native):
        return None
    author, model = native.split(".", 1)
    if author == "deepseek":
        model = f"deepseek-{model}"
    return mapped_or_canonical_model_id(f"{'mistralai' if author == 'mistral' else author}/{model}", {})


def _rate(terms: object, now: datetime) -> int:
    if not isinstance(terms, dict) or not terms:
        raise RuntimeError("bedrock: missing on-demand terms")
    applicable = []
    for term in terms.values():
        try:
            effective = datetime.fromisoformat(term["effectiveDate"].replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("bedrock: invalid effective date") from exc
        if effective.tzinfo is None:
            raise RuntimeError("bedrock: unzoned effective date")
        if effective <= now:
            applicable.append((effective, term))
    if not applicable:
        raise RuntimeError("bedrock: no currently effective price")
    latest = max(date for date, _ in applicable)
    rates = []
    for date, term in applicable:
        if date != latest:
            continue
        dimensions = term.get("priceDimensions")
        if not isinstance(dimensions, dict) or len(dimensions) != 1:
            raise RuntimeError("bedrock: tiered or missing price dimension")
        dim = next(iter(dimensions.values()))
        multiplier = {"1K tokens": 1_000_000_000, "1M tokens": 1_000_000}.get(dim.get("unit"))
        if multiplier is None or dim.get("beginRange") != "0" or dim.get("endRange") != "Inf":
            raise RuntimeError("bedrock: unsupported token price units/range")
        try:
            amount = Decimal(dim["pricePerUnit"]["USD"]) * multiplier
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise RuntimeError("bedrock: missing or invalid USD price") from exc
        if not amount.is_finite() or not 0 < amount <= 1_000_000_000 or amount != amount.to_integral_value():
            raise RuntimeError("bedrock: invalid token price")
        rates.append(int(amount))
    if len(set(rates)) != 1:
        raise RuntimeError("bedrock: conflicting currently effective prices")
    return rates[0]


def parse_prices(payload: object, *, now: datetime | None = None) -> dict[str, ModelPrice]:
    if not isinstance(payload, dict) or not isinstance(payload.get("products"), dict):
        raise RuntimeError("bedrock: invalid AWS pricing document")
    terms = payload.get("terms", {}).get("OnDemand", {})
    meters: dict[str, dict[str, int]] = {}
    unsupported: set[str] = set()
    for sku, product in payload["products"].items():
        attrs = product.get("attributes", {})
        if attrs.get("regionCode") != REGION:
            continue
        usage = attrs.get("usagetype", "")
        # Cache-write tariffs require separate billing support; never treat them
        # as ordinary input or infer a cache TTL from another provider.
        write = re.fullmatch(r"USE1-(.+)-mantle-cache-write-tokens-.*standard", usage)
        if write:
            unsupported.add(write[1])
        match = _SKU.fullmatch(usage)
        if match is None:
            continue
        native, meter = match.groups()
        value = _rate(terms.get(sku), now or datetime.now(UTC))
        existing = meters.setdefault(native, {}).get(meter)
        if existing is not None and existing != value:
            raise RuntimeError(f"bedrock: conflicting {native} {meter} prices")
        meters[native][meter] = value
    prices = {
        native: ModelPrice(values["input"], values["output"], prompt_cached_micro_per_m=values.get("cache-read"))
        for native, values in meters.items()
        if native not in unsupported and "input" in values and "output" in values
    }
    if not prices or (errors := validate(prices, [])):
        raise RuntimeError("bedrock: no valid exact standard prices" if not prices else "; ".join(errors))
    return prices


def discover(catalog: object, tariffs: object) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(catalog, dict) or not isinstance(catalog.get("data"), list) or not catalog["data"]:
        raise RuntimeError("bedrock: empty or invalid model catalog")
    native_prices = parse_prices(tariffs)
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for source in catalog["data"]:
        native = source.get("id") if isinstance(source, dict) else None
        model_id = canonical_model_id(native) if isinstance(native, str) else None
        if model_id is None or model_id in rows:
            raise RuntimeError("bedrock: invalid or duplicate native model")
        # Mantle does not publish token limits/capabilities in its catalog. Use
        # an intentionally conservative route cap until AWS provides metadata;
        # do not inherit another provider's context/vision/tool guarantees.
        row: dict[str, Any] = {
            "id": model_id, "upstream_id": native, "display_name": native.split(".", 1)[1],
            "context_length": 4096, "max_output_tokens": 1024,
            "input_modalities": ["text"], "output_modalities": ["text"],
            "supported_features": ["streaming"], "endpoints": ["chat/completions"],
        }
        rows[model_id] = row
        if source.get("status") != "available":
            row.update(routable=False, routable_reason="provider-unavailable")
        elif native not in native_prices:
            row.update(routable=False, routable_reason="price-unavailable")
        else:
            prices[model_id] = native_prices[native]
    if not prices:
        raise RuntimeError("bedrock: no available models with exact prices")
    return prices, rows


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603
    _DISCOVERED_ROWS = {}
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("BEDROCK_API_KEY", "").strip()
    if not key:
        raise RuntimeError("bedrock: BEDROCK_API_KEY is required")
    prices, rows = discover(fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"}), fetch_json(PRICING_URL))
    with ThreadPoolExecutor(max_workers=4) as pool:
        limits = pool.map(lambda model: fetch_model_limits(rows[model]["upstream_id"]), prices)
        for model, metadata in zip(prices, limits, strict=True):
            rows[model].update(metadata)
    checked = sorted(models_requiring_canary(MANIFEST_PATH, set(prices)))
    with ThreadPoolExecutor(max_workers=4) as pool:
        outcomes = list(pool.map(lambda model: probe_openai_chat(
            base_url=BASE_URL, api_key=key, model=rows[model]["upstream_id"],
            max_tokens=512, require_message=True, require_usage=True, prompt="Reply exactly PONG.",
        ), checked))
    healthy = {model for model, ok in zip(checked, outcomes, strict=True) if ok}
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    UPSTREAM_ID_MAP.update({model: row["upstream_id"] for model, row in rows.items()})
    _DISCOVERED_ROWS = rows
    return ProviderPricingResult(slug=SLUG, prices=prices, source="api", fetched_url=PRICING_URL,
        notes=[f"{len(prices)} exact-priced models; {len(healthy)}/{len(checked)} canaries passed"])


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _DISCOVERED_ROWS:
        raise RuntimeError("bedrock: successful discovery required before publication")
    return write_discovered_chat_manifest(result, manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_ROWS, source_url=URL, pricing_source_url=PRICING_URL,
        operator_hold_reasons={model: row["routable_reason"] for model, row in _DISCOVERED_ROWS.items()
            if row.get("routable") is False and row.get("routable_reason") != "provider-canary-failed"})
