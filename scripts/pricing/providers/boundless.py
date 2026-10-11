"""Boundless live catalog intersected with its public per-million USD rate cards."""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_html, fetch_json, validate
from scripts.pricing.manifest import (
    apply_canary_results,
    models_requiring_canary,
    write_discovered_chat_manifest,
)
from scripts.pricing.model_ids import (
    canonicalize_native_model_id,
    canonicalize_unqualified_model_id,
)
from scripts.pricing.openai_catalog import probe_openai_chat

SLUG = "boundless"
BASE_URL = "https://api.inference.boundless.network/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://inference.boundless.network/models"
MANIFEST_PATH = Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/boundless.json"
MANIFEST_STALE_FALLBACK = True
UPSTREAM_ID_MAP: dict[str, str] = {}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}
_ALIASES = {
    "qwen3.6": "qwen/qwen3.6-35b-a3b",
    "nemotron3-super": "nvidia/nemotron-3-super",
    # The provider's machine-readable model description explicitly names 0731.
    "dsv4": "deepseek/deepseek-v4-flash-0731",
}


def canonical_model_id(native_id: str) -> str | None:
    if native_id == "convaiinnovations/laya":
        return None  # Separate unpriced decision API, not chat completions.
    if native_id in _ALIASES:
        return _ALIASES[native_id]
    for prefix, author in (("gpt-", "openai"), ("claude-", "anthropic")):
        if native_id.startswith(prefix):
            return f"{author}/{native_id}"
    return canonicalize_unqualified_model_id(native_id) or canonicalize_native_model_id(native_id) or f"boundless/{native_id}"


def _usd(value: str) -> int:
    if not re.fullmatch(r"\$[0-9]+(?:\.[0-9]+)?", value):
        raise RuntimeError("boundless: expected an exact USD rate")
    amount = Decimal(value[1:]) * 1_000_000
    if amount != amount.to_integral_value():
        raise RuntimeError("boundless: unsupported price precision")
    return int(amount)


def parse_cards(html: str) -> dict[str, tuple[ModelPrice, dict[str, Any]]]:
    soup = BeautifulSoup(html, "html.parser")
    cards: dict[str, tuple[ModelPrice, dict[str, Any]]] = {}
    products = []
    # The visible cards round tiny cache rates to $0.00. JSON-LD preserves
    # exact rates (e.g. MiMo Pro $0.435 input / $0.0036 cache-read).
    for script in soup.select('script[type="application/ld+json"]'):
        document = json.loads(script.get_text())
        if isinstance(document, dict) and document.get("@type") == "ItemList":
            products.extend(item["item"] for item in document.get("itemListElement", [])
                            if isinstance(item, dict) and isinstance(item.get("item"), dict)
                            and item["item"].get("@type") == "Product")
    for card in products:
        native = card.get("sku")
        if not native or native in cards:
            raise RuntimeError("boundless: missing or duplicate rate-card identity")
        if canonical_model_id(native) is None:
            continue
        fields: dict[str, int] = {}
        for offer in card.get("offers", []):
            spec = offer.get("priceSpecification", {})
            unit = spec.get("unitText")
            if not isinstance(unit, str) or unit in fields or spec.get("priceCurrency") != "USD":
                raise RuntimeError("boundless: invalid price units or currency")
            fields[unit] = _usd(f'${spec.get("price")}')
        units = ("per 1M input tokens", "per 1M output tokens", "per 1M cached input tokens")
        if set(fields) != set(units):
            raise RuntimeError("boundless: missing or unexpected price units")
        prompt, output, cached = (fields[unit] for unit in units)
        if prompt <= 0 or output <= 0 or cached > prompt:
            raise RuntimeError("boundless: invalid token prices")
        metadata = {item["name"]: item["value"] for item in card.get("additionalProperty", [])}
        if metadata.get("serving") is not True:
            continue
        features = set(str(metadata.get("supports", "")).split(", "))
        cards[native] = (
            ModelPrice(prompt, output, prompt_cached_micro_per_m=cached),
            {
                "display_name": card.get("name") or native,
                "input_modalities": ["text"] + [kind for kind in ("image", "audio", "video") if f"{kind} input" in features],
                "output_modalities": ["text"],
                "supported_features": ["chat", "completion", "streaming", "prompt_caching"]
                + (["reasoning"] if "reasoning" in features else [])
                + (["tools"] if "tool-use" in features else []),
            },
        )
    if not cards:
        raise RuntimeError("boundless: no priced cards; refusing empty publication")
    return cards


def discover(payload: object, html: str) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("boundless: catalog has no data list")
    cards = parse_cards(html)
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for source in payload["data"]:
        native = source.get("id") if isinstance(source, dict) else None
        if not isinstance(native, str) or not native:
            raise RuntimeError("boundless: invalid catalog ID")
        model_id = canonical_model_id(native)
        if model_id is None:
            continue
        if model_id in rows:
            raise RuntimeError("boundless: duplicate canonical model ID")
        row: dict[str, Any] = {"id": model_id, "upstream_id": native, "endpoints": ["chat/completions"]}
        if native in cards:
            price, metadata = cards[native]
            context = source.get("context_length")
            if type(context) is not int or context <= 0:
                raise RuntimeError("boundless: invalid context length")
            api_price = source.get("pricing", {})
            if not isinstance(api_price, dict) or any(
                Decimal(str(api_price.get(field))) * 1_000_000 != amount
                for field, amount in (("input", price.prompt_micro_per_m), ("output", price.completion_micro_per_m))
            ):
                raise RuntimeError("boundless: API and published prices disagree")
            row.update(metadata, context_length=context)
            prices[model_id] = price
        else:
            row.update(routable=False, routable_reason="awaiting-price")
        rows[model_id] = row
    if not prices or (errors := validate(prices, [])):
        raise RuntimeError("boundless: no priced live models" if not prices else "; ".join(errors))
    return prices, rows


def fetch() -> ProviderPricingResult:
    _DISCOVERED_ROWS.clear()
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("BOUNDLESS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("BOUNDLESS_API_KEY is required for discovery")
    prices, rows = discover(fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"}), fetch_html(PRICING_URL))
    checked = models_requiring_canary(MANIFEST_PATH, prices)
    healthy = {model_id for model_id in checked if probe_openai_chat(
        base_url=BASE_URL, api_key=key, model=rows[model_id]["upstream_id"],
        max_tokens=512, expected_content="PONG", require_usage=True,
        prompt="Reply with exactly PONG and nothing else.",
    )}
    apply_canary_results(rows, checked_model_ids=checked, healthy_model_ids=healthy)
    _DISCOVERED_ROWS.update(rows)
    UPSTREAM_ID_MAP.update({model_id: row["upstream_id"] for model_id, row in rows.items()})
    return ProviderPricingResult(slug=SLUG, prices=prices, source="api", fetched_url=PRICING_URL,
                                 notes=[f"{len(prices)} priced models; {len(healthy)}/{len(checked)} canaries passed"])


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    if not _DISCOVERED_ROWS:
        raise RuntimeError("boundless: fetch must succeed before writing manifest")
    return write_discovered_chat_manifest(result, manifest_path=MANIFEST_PATH, discovered_rows=_DISCOVERED_ROWS,
                                         source_url=URL, pricing_source_url=PRICING_URL)
