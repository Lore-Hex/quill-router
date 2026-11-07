"""Cohere first-party embedding pricing refresh."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.pricing.base import (
    ModelPrice,
    ProviderPricingResult,
    fetch_html,
    fetch_provider,
    normalize_parser_input,
)
from scripts.pricing.manifest import write_embedding_provider_manifest

SLUG = "cohere"
URL = "https://cohere.com/pricing"
EXPECTED_MODELS = ["cohere/embed-v4.0"]
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
        return ProviderPricingResult(
            slug=SLUG,
            prices={EMBED_V4_MODEL_ID: ModelPrice(EMBED_V4_PINNED_MICRO_PER_M, 0)},
            source=PINNED_SOURCE,
            fetched_url=URL,
            notes=["embed-v4.0 pinned at its last published rate; cohere.com/pricing publishes no Embed 4 rate"],
        )
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    notes = write_embedding_provider_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        required_model_ids=frozenset(EXPECTED_MODELS),
    )
    if result.source == PINNED_SOURCE:
        raw = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        for row in raw["models"]:
            if isinstance(row, dict) and row.get("id") == EMBED_V4_MODEL_ID:
                row["pricing_source"] = PINNED_PRICING_SOURCE
        MANIFEST_PATH.write_text(json.dumps(raw, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return notes
