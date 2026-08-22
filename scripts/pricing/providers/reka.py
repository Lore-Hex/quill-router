"""Reka authenticated multimodal chat catalog and official prices."""

import re
from decimal import Decimal
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.pricing.base import (
    ModelPrice,
    apply_required_model_price_aliases,
    fetch_html,
    runtime_required_models,
)
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "reka"
BASE_URL = "https://api.reka.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://docs.reka.ai/pricing"
PRICING_FEED_URL = f"{PRICING_URL}.md"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/reka.json"
)
EXPLICIT_MODEL_MAP = {
    "reka-edge": "reka/reka-edge",
    "reka-edge-2603": "reka/reka-edge-2603",
    "reka-flash": "reka/reka-flash",
    "reka-core": "reka/reka-core",
}

_DISPLAY_MODEL_MAP = {
    "Reka Edge": "reka/reka-edge",
    "Reka Flash": "reka/reka-flash",
    "Reka Core": "reka/reka-core",
}
_PERSISTED_PRICE_ALIASES = {
    "reka/reka-edge-2603": "reka/reka-edge",
}


def _micro_per_m(text: str) -> int:
    match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", text)
    if match is None:
        raise RuntimeError(f"reka: malformed token price {text!r}")
    return int(Decimal(match.group(1)) * Decimal(1_000_000))


def _parse_pricing(source: str) -> dict[str, ModelPrice]:
    soup = BeautifulSoup(source, "html.parser")
    prices: dict[str, ModelPrice] = {}

    def add_price(display_text: str, prompt_text: str, completion_text: str) -> None:
        model_id = next(
            (
                candidate_id
                for display_name, candidate_id in _DISPLAY_MODEL_MAP.items()
                if display_text.startswith(display_name)
            ),
            None,
        )
        if model_id is None:
            return
        prices[model_id] = ModelPrice(
            _micro_per_m(prompt_text),
            _micro_per_m(completion_text),
        )

    for row in soup.select("tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 3:
            continue
        add_price(
            cells[0].get_text(" ", strip=True),
            cells[1].get_text(" ", strip=True),
            cells[2].get_text(" ", strip=True),
        )

    # Fern publishes a stable Markdown representation even when its HTML table
    # is client-rendered. Parsing both keeps fixtures readable and production
    # independent of the documentation site's JavaScript markup.
    for line in source.splitlines():
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        add_price(
            BeautifulSoup(cells[0], "html.parser").get_text(" ", strip=True),
            cells[1],
            cells[2],
        )
    return prices


def _load_prices() -> dict[str, ModelPrice]:
    prices = _parse_pricing(fetch_html(PRICING_FEED_URL))
    required = frozenset(_PERSISTED_PRICE_ALIASES) | runtime_required_models(SLUG)
    expanded, _applied = apply_required_model_price_aliases(
        prices,
        required,
        _PERSISTED_PRICE_ALIASES,
    )
    return expanded


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env=("REKA_API_KEY", "REKA_PERSONALAPI_KEY"),
        explicit_model_map=EXPLICIT_MODEL_MAP,
        price_loader=_load_prices,
        expected_models=("reka/reka-edge-2603", "reka/reka-flash"),
        pricing_source_url=PRICING_URL,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
