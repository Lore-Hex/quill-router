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
    # The repository-wide ID, and the one Boundless's own rate card names
    # (sku nvidia/nemotron-3-super-120b-a12b) since 2026-11.
    "nemotron3-super": "nvidia/nemotron-3-super-120b-a12b",
    # The provider's machine-readable model description explicitly names 0731.
    "dsv4": "deepseek/deepseek-v4-flash-0731",
    # Short alias of the card named google/gemma-4-31b-it (2026-11).
    "gemma4-31b": "google/gemma-4-31b-it",
}


def canonical_model_id(native_id: str) -> str | None:
    if native_id == "convaiinnovations/laya":
        return None  # Separate unpriced decision API, not chat completions.
    if native_id in _ALIASES:
        return _ALIASES[native_id]
    if "/" in native_id:
        # Already qualified (the 2026-11 rate cards name every model this
        # way); the unqualified helper would prefix the author a second time.
        return canonicalize_native_model_id(native_id) or native_id
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


def parse_cards(html: str) -> dict[str, tuple[ModelPrice, dict[str, Any], str]]:
    """Served rate cards keyed by canonical model ID: (price, metadata, sku)."""
    soup = BeautifulSoup(html, "html.parser")
    cards: dict[str, tuple[ModelPrice, dict[str, Any], str]] = {}
    skus: set[str] = set()
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
        if not isinstance(native, str) or not native or native in skus:
            raise RuntimeError("boundless: missing or duplicate rate-card identity")
        skus.add(native)
        model_id = canonical_model_id(native)
        if model_id is None:
            continue
        metadata = {item["name"]: item["value"] for item in card.get("additionalProperty", [])}
        # A card Boundless does not serve publishes no rate card to validate:
        # the free-tier alias minimax-m3-free (2026-11) carries an offer with
        # no priceSpecification. Skipping it never admits an unpriced route.
        if metadata.get("serving") is not True:
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
        features = set(str(metadata.get("supports", "")).split(", "))
        if model_id in cards:
            # Two served, priced cards for one model: no basis to pick a price.
            raise RuntimeError("boundless: duplicate canonical model ID among served rate cards")
        cards[model_id] = (
            ModelPrice(prompt, output, prompt_cached_micro_per_m=cached),
            {
                "display_name": card.get("name") or native,
                "input_modalities": ["text"] + [kind for kind in ("image", "audio", "video") if f"{kind} input" in features],
                "output_modalities": ["text"],
                "supported_features": ["chat", "completion", "streaming", "prompt_caching"]
                + (["reasoning"] if "reasoning" in features else [])
                + (["tools"] if "tool-use" in features else []),
            },
            native,
        )
    if not cards:
        raise RuntimeError("boundless: no priced cards; refusing empty publication")
    return cards


def discover(payload: object, html: str) -> tuple[dict[str, ModelPrice], dict[str, dict[str, Any]]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RuntimeError("boundless: catalog has no data list")
    cards = parse_cards(html)
    natives: set[str] = set()
    by_model: dict[str, list[dict[str, Any]]] = {}
    for source in payload["data"]:
        native = source.get("id") if isinstance(source, dict) else None
        if not isinstance(native, str) or not native or native in natives:
            raise RuntimeError("boundless: invalid or duplicate catalog ID")
        natives.add(native)
        model_id = canonical_model_id(native)
        if model_id is not None:
            by_model.setdefault(model_id, []).append(source)
    prices: dict[str, ModelPrice] = {}
    rows: dict[str, dict[str, Any]] = {}
    for model_id, sources in by_model.items():
        card = cards.get(model_id)
        if len(sources) > 1:
            # Boundless lists a legacy short identifier beside the qualified
            # one its served rate card names (2026-11: dsv4 beside
            # deepseek/deepseek-v4-flash-0731). The card's identifier is the
            # upstream; without exactly one such match the mapping is
            # ambiguous and fails closed.
            sources = [source for source in sources if card is not None and source["id"] == card[2]]
            if len(sources) != 1:
                raise RuntimeError("boundless: duplicate canonical model ID")
        source = sources[0]
        native = source["id"]
        row: dict[str, Any] = {"id": model_id, "upstream_id": native, "endpoints": ["chat/completions"]}
        if card is not None:
            price, metadata, _sku = card
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


def _upstream_changed(rows: dict[str, dict[str, Any]]) -> frozenset[str]:
    try:
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset()
    previous = {
        row["id"]: row.get("upstream_id")
        for row in manifest.get("models", []) if isinstance(row, dict) and isinstance(row.get("id"), str)
    } if isinstance(manifest, dict) else {}
    return frozenset(
        model_id for model_id, row in rows.items()
        if model_id in previous and previous[model_id] != row["upstream_id"]
    )


def fetch() -> ProviderPricingResult:
    _DISCOVERED_ROWS.clear()
    UPSTREAM_ID_MAP.clear()
    key = os.environ.get("BOUNDLESS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("BOUNDLESS_API_KEY is required for discovery")
    prices, rows = discover(fetch_json(URL, extra_headers={"Authorization": f"Bearer {key}"}), fetch_html(PRICING_URL))
    # A route whose upstream identifier changed is re-probed like a new one.
    checked = models_requiring_canary(MANIFEST_PATH, prices) | (_upstream_changed(rows) & frozenset(prices))
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
