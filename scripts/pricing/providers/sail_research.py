"""Sail Research authenticated catalog joined to first-party ASAP prices."""

import re
from decimal import Decimal
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from scripts.pricing.base import ModelPrice, fetch_html
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "sail-research"
BASE_URL = "https://api.sailresearch.com/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://docs.sailresearch.com/pricing"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/sail-research.json"
)
EXPLICIT_MODEL_MAP = {
    "zai-org/GLM-5.2-FP8": "z-ai/glm-5.2",
    "deepseek/deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
    "moonshotai/Kimi-K2.6": "moonshotai/kimi-k2.6",
    "openai/gpt-oss-120b": "openai/gpt-oss-120b",
    "google/gemma-4-31B-it": "google/gemma-4-31b-it",
    "nvidia/Gemma-4-31B-IT-NVFP4": "google/gemma-4-31b-it-nvfp4",
}


def _micro_per_m(node: Tag, *, model_id: str, axis: str) -> int:
    match = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", node.get_text(" ", strip=True))
    if match is None:
        raise RuntimeError(f"sail-research: malformed {axis} price for {model_id}")
    return int(Decimal(match.group(1)) * Decimal(1_000_000))


def _parse_pricing(source: str) -> dict[str, ModelPrice]:
    soup = BeautifulSoup(source, "html.parser")
    prices: dict[str, ModelPrice] = {}
    for group in soup.select("tbody[data-model]"):
        native_id = str(group.get("data-model") or "")
        model_id = EXPLICIT_MODEL_MAP.get(native_id)
        if model_id is None:
            continue
        asap = group.select_one('tr [data-axis="Input"][data-window="asap"]')
        cached = group.select_one('tr [data-axis="Cached"][data-window="asap"]')
        output = group.select_one('tr [data-axis="Output"][data-window="asap"]')
        if not isinstance(asap, Tag) or not isinstance(cached, Tag) or not isinstance(output, Tag):
            raise RuntimeError(f"sail-research: no complete ASAP price for {native_id}")
        prices[model_id] = ModelPrice(
            _micro_per_m(asap, model_id=model_id, axis="input"),
            _micro_per_m(output, model_id=model_id, axis="output"),
            prompt_cached_micro_per_m=_micro_per_m(
                cached,
                model_id=model_id,
                axis="cached",
            ),
        )
    return prices


def _load_prices() -> dict[str, ModelPrice]:
    return _parse_pricing(fetch_html(PRICING_URL))


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="SAIL_RESEARCH_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        price_loader=_load_prices,
        expected_models=("z-ai/glm-5.2", "deepseek/deepseek-v4-flash-0731"),
        pricing_source_url=PRICING_URL,
        canary_max_tokens=64,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
