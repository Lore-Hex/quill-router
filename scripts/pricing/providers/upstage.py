"""Upstage Solar authenticated catalog joined to official token prices."""

import re
from decimal import Decimal
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, fetch_html
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "upstage"
BASE_URL = "https://api.upstage.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://www.upstage.ai/pricing/api"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/upstage.json"
)
MANIFEST_STALE_FALLBACK = True
EXPLICIT_MODEL_MAP = {
    "solar-pro2": "upstage/solar-pro2",
    "solar-pro3": "upstage/solar-pro3",
    "solar-pro4": "upstage/solar-pro4",
}

_DISPLAY_MODEL_MAP = {
    "Solar Pro 2": "upstage/solar-pro2",
    "Solar Pro 3": "upstage/solar-pro3",
    "Solar Pro 4": "upstage/solar-pro4",
}


def _micro_per_m(raw: str) -> int:
    return int(Decimal(raw) * Decimal(1_000_000))


def _parse_pricing(source: str) -> dict[str, ModelPrice]:
    soup = BeautifulSoup(source, "html.parser")
    prices: dict[str, ModelPrice] = {}
    for card in soup.select(".pricing-card-v2"):
        heading = card.find("h4")
        if heading is None:
            continue
        model_id = _DISPLAY_MODEL_MAP.get(heading.get_text(" ", strip=True))
        if model_id is None:
            continue
        axes: dict[str, int] = {}
        for feature in card.select(".pricing-feature-v2"):
            text = feature.get_text(" ", strip=True)
            match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)\s*/\s*1M tokens", text)
            if match is None:
                continue
            if text.startswith("Input(Cached)"):
                axes["cached"] = _micro_per_m(match.group(1))
            elif text.startswith("Input"):
                axes["input"] = _micro_per_m(match.group(1))
            elif text.startswith("Output"):
                axes["output"] = _micro_per_m(match.group(1))
        if set(axes) != {"input", "cached", "output"}:
            raise RuntimeError(f"upstage: incomplete pricing card for {model_id}")
        prices[model_id] = ModelPrice(
            axes["input"],
            axes["output"],
            prompt_cached_micro_per_m=axes["cached"],
        )
    return prices


def _load_prices() -> dict[str, ModelPrice]:
    return _parse_pricing(fetch_html(PRICING_URL))


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="UPSTAGE_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        price_loader=_load_prices,
        expected_models=("upstage/solar-pro4",),
        pricing_source_url=PRICING_URL,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
