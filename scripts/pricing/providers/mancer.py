"""Mancer catalog priced conservatively at its least-discounted credit pack."""

import re
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from scripts.pricing.base import ModelPrice, fetch_html, fetch_json
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "mancer"
BASE_URL = "https://mancer.tech/oai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://mancer.tech/models"
CREDIT_PACKS_URL = "https://mancer.tech/internal/api/get_pricing"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/mancer.json"
)
EXPLICIT_MODEL_MAP = {
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
    "gemma-4-31b-it": "google/gemma-4-31b-it",
    "glm-4.7": "z-ai/glm-4.7",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "mythomax": "gryphe/mythomax-l2-13b",
    "remm-slerp": "undi95/remm-slerp-l2-13b",
}

_DISPLAY_MODEL_MAP = {
    "DeepSeek V4 Flash 0731": "deepseek/deepseek-v4-flash-0731",
    "DeepSeek V4 Flash": "deepseek/deepseek-v4-flash",
    "Gemma 4 31B Instruct": "google/gemma-4-31b-it",
    "GLM 4.7": "z-ai/glm-4.7",
    "GPT OSS 120B": "openai/gpt-oss-120b",
    "MythoMax": "gryphe/mythomax-l2-13b",
    "ReMM-SLerp": "undi95/remm-slerp-l2-13b",
}


def _max_microdollars_per_million_credits(payload: object) -> Decimal:
    if not isinstance(payload, list):
        raise RuntimeError("mancer: credit pack API did not return a list")
    rates: list[Decimal] = []
    for row in payload:
        if not isinstance(row, dict) or row.get("can_purchase") is not True:
            continue
        price = Decimal(str(row.get("price") or "0"))
        credits = Decimal(str(row.get("credits") or "0"))
        if price <= 0 or credits <= 0:
            continue
        rates.append(price * Decimal(1_000_000_000_000) / credits)
    if not rates:
        raise RuntimeError("mancer: no purchasable credit pack has a usable price")
    return max(rates)


def _credit_price(node: Tag, *, axis: str, display_name: str) -> str:
    raw = node.get_text(" ", strip=True)
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)", raw)
    if match is None:
        raise RuntimeError(
            f"mancer: malformed {axis} token credit price for {display_name}"
        )
    return match.group(1)


def _parse_pricing(source: str, credit_packs: object) -> dict[str, ModelPrice]:
    micro_per_m_credits = _max_microdollars_per_million_credits(credit_packs)
    soup = BeautifulSoup(source, "html.parser")
    price_headers = {
        cell.get_text(" ", strip=True).casefold()
        for cell in soup.select("table th")
    }
    if not any("price" in header and "credit" in header for header in price_headers):
        raise RuntimeError("mancer: model table does not label prices in credits")
    prices: dict[str, ModelPrice] = {}
    for row in soup.select("table tr"):
        cells = row.find_all(["th", "td"])
        if len(cells) < 3:
            continue
        label = cells[0].get_text(" ", strip=True)
        display_name = next(
            (name for name in _DISPLAY_MODEL_MAP if label.startswith(name)),
            None,
        )
        if display_name is None:
            continue
        prompt_node = cells[2].find("x-intag")
        completion_node = cells[2].find("x-outtag")
        if not isinstance(prompt_node, Tag) or not isinstance(completion_node, Tag):
            raise RuntimeError(f"mancer: malformed token credit prices for {display_name}")

        def converted(raw: str) -> int:
            return int(
                (Decimal(raw) * micro_per_m_credits).to_integral_value(rounding=ROUND_CEILING)
            )

        prices[_DISPLAY_MODEL_MAP[display_name]] = ModelPrice(
            converted(
                _credit_price(prompt_node, axis="input", display_name=display_name)
            ),
            converted(
                _credit_price(completion_node, axis="output", display_name=display_name)
            ),
        )
    return prices


def _load_prices() -> dict[str, ModelPrice]:
    return _parse_pricing(fetch_html(PRICING_URL), fetch_json(CREDIT_PACKS_URL))


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="MANCER_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        price_loader=_load_prices,
        expected_models=("deepseek/deepseek-v4-flash-0731", "z-ai/glm-4.7"),
        pricing_source_url=PRICING_URL,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
