"""Cohere first-party embedding pricing refresh."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_html,
    fetch_provider,
    normalize_parser_input,
)
from scripts.pricing.manifest import write_embedding_provider_manifest
from scripts.pricing.parsers import cohere as parser

SLUG = "cohere"
URL = "https://cohere.com/pricing"
EXPECTED_MODELS = ["cohere/embed-v4.0"]
# Pricing labels are not API identifiers. Add entries only with API evidence.
RERANK_API_IDS = {
    "Rerank 5 Fast": "rerank-v5.0-fast",
    "Rerank 5 Pro": "rerank-v5.0-pro",
}
RERANK_UPSTREAM_IDS = {
    "cohere/" + label.lower().replace(" ", "-"): api_id
    for label, api_id in RERANK_API_IDS.items()
}
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "trusted_router"
    / "data"
    / "provider_models"
    / "cohere.json"
)

# Since 2026-11 cohere.com/pricing prices only Embed 5 Fast and Embed 5 Pro.
# Embed 4 keeps a model card with no rate, while docs.cohere.com still lists
# embed-v4.0 as a served model and its deprecations page does not retire it.
# Like the Embed v3 rows, embed-v4.0 stays at its last published first-party
# rate ($0.12 per 1M tokens, cohere.com/pricing until 2026-09-30) for as long
# as Cohere serves it without publishing one. A published Embed 4 rate on the
# page always wins over this pin.
EMBED_V4_MODEL_ID = "cohere/embed-v4.0"
EMBED_V4_PINNED_MICRO_PER_M = 120_000
PINNED_SOURCE = "legacy_pinned"
PINNED_PRICING_SOURCE = "legacy-pinned"

_PRICED_RECORD = re.compile(r'"modelName":"([^"]+)","per":"1M tokens"')


def embed4_rate_unpublished(html: str) -> bool:
    """True when the pricing payload prices Embed models but not Embed 4.

    A page without that payload is a layout change, not an unpublished rate:
    it goes to the normal parser and self-heal path instead of the pin.
    """
    priced = set(_PRICED_RECORD.findall(html.replace(r'\"', '"')))
    return "Embed 4" not in priced and any(name.startswith("Embed ") for name in priced)


def fetch() -> ProviderPricingResult:
    html = normalize_parser_input(fetch_html(URL))
    if embed4_rate_unpublished(html):
        result = ProviderPricingResult(
            slug=SLUG,
            prices={
                EMBED_V4_MODEL_ID: ModelPrice(EMBED_V4_PINNED_MICRO_PER_M, 0),
                **{key: ModelPrice(value["prompt_micro_per_m"], 0)
                   for key, value in parser.parse_rerank(html).items()},
            },
            source=PINNED_SOURCE,
            fetched_url=URL,
            notes=["embed-v4.0 pinned at its last published rate; cohere.com/pricing publishes no Embed 4 rate"],
        )
    else:
        result = fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
    for model_id in list(result.prices):
        if model_id.startswith("cohere/rerank-") and model_id not in RERANK_UPSTREAM_IDS:
            del result.prices[model_id]
            result.notes.append(f"skipped {model_id}: no documented API id for pricing label")
    for name, unit in re.findall(r'"modelName":"(Rerank [^"]+)","per":"([^"]+)"', html.replace(r'\"', '"')):
        model_id = "cohere/" + name.lower().replace(" ", "-")
        if model_id not in result.prices:
            result.notes.append(f"skipped {name}: no positive per-token price (source unit: {unit})")
    result.price_index_model_ids = frozenset(key for key in result.prices
                                             if not key.startswith("cohere/rerank-"))
    return result


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    unknown = {key for key in result.prices
               if key.startswith("cohere/rerank-") and key not in RERANK_UPSTREAM_IDS}
    result = replace(result, prices={key: price for key, price in result.prices.items()
                                    if key not in unknown})
    notes = write_embedding_provider_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        required_model_ids=frozenset(EXPECTED_MODELS),
        rerank_model_ids=frozenset(key for key in result.prices if key.startswith("cohere/rerank-")),
    )
    notes.extend(f"skipped {key}: no documented API id for pricing label" for key in sorted(unknown))
    raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for row in raw["models"]:
        if not isinstance(row, dict):
            continue
        if row.get("model_type") == "rerank" and row.get("id") in RERANK_UPSTREAM_IDS:
            row["upstream_id"] = RERANK_UPSTREAM_IDS[row["id"]]
        if result.source == PINNED_SOURCE and row.get("id") == EMBED_V4_MODEL_ID:
            row["pricing_source"] = PINNED_PRICING_SOURCE
    MANIFEST_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return notes
