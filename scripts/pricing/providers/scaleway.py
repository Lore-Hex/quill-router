"""Scaleway authenticated availability with first-party EUR token prices."""

from __future__ import annotations

import re
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, fetch_html
from scripts.pricing.providers._direct_openai import (
    DirectOpenAIProvider,
    DirectOpenAIProviderSpec,
)

SLUG = "scaleway"
BASE_URL = "https://api.scaleway.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://www.scaleway.com/en/pricing/model-as-a-service/"
ECB_FX_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
FX_RESERVE = Decimal("1.05")
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/scaleway.json"
)
MANIFEST_STALE_FALLBACK = True

MODEL_MAP = {
    "llama-3.3-70b-instruct": "meta-llama/llama-3.3-70b-instruct",
    "pixtral-12b-2409": "mistralai/pixtral-12b-2409",
    "qwen3-235b-a22b-instruct-2507": "qwen/qwen3-235b-a22b-instruct-2507",
    "mistral-small-3.2-24b-instruct-2506": "mistralai/mistral-small-3.2-24b-instruct-2506",
    "qwen3-coder-30b-a3b-instruct": "qwen/qwen3-coder-30b-a3b-instruct",
    "gpt-oss-120b": "openai/gpt-oss-120b",
    "qwen3.5-397b-a17b": "qwen/qwen3.5-397b-a17b",
    "gemma-4-26b-a4b-it": "google/gemma-4-26b-a4b-it",
    "qwen3.6-35b-a3b": "qwen/qwen3.6-35b-a3b",
    "mistral-medium-3.5-128b": "mistralai/mistral-medium-3.5-128b",
    "glm-5.2": "z-ai/glm-5.2",
    "deepseek-v4-flash-0731": "deepseek/deepseek-v4-flash-0731",
}

_EURO_RE = re.compile(r"€\s*([0-9]+(?:\.[0-9]+)?)")
_USD_RATE_RE = re.compile(r"currency=['\"]USD['\"]\s+rate=['\"]([0-9.]+)['\"]")


def _usd_per_eur(xml: str) -> Decimal:
    match = _USD_RATE_RE.search(xml)
    if match is None:
        raise RuntimeError("scaleway: ECB feed has no USD/EUR rate")
    try:
        rate = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise RuntimeError("scaleway: ECB USD/EUR rate is invalid") from exc
    if not rate.is_finite() or rate <= 0:
        raise RuntimeError("scaleway: ECB USD/EUR rate must be positive")
    return rate


def _microdollars_per_million(eur: Decimal, usd_per_eur: Decimal) -> int:
    return int(
        (eur * usd_per_eur * FX_RESERVE * Decimal("1000000")).to_integral_value(
            ROUND_CEILING
        )
    )


def _parse_prices(html: str, *, usd_per_eur: Decimal) -> dict[str, ModelPrice]:
    prices: dict[str, ModelPrice] = {}
    for row in BeautifulSoup(html, "html.parser").find_all("tr"):
        cells = row.find_all("td")
        if len(cells) < 4:
            continue
        native_id = cells[0].get_text(" ", strip=True)
        model_id = MODEL_MAP.get(native_id)
        if model_id is None:
            continue
        input_values = [Decimal(value) for value in _EURO_RE.findall(str(cells[2]))]
        output_values = [Decimal(value) for value in _EURO_RE.findall(str(cells[3]))]
        if not input_values or not output_values:
            continue
        prices[model_id] = ModelPrice(
            _microdollars_per_million(input_values[0], usd_per_eur),
            _microdollars_per_million(output_values[0], usd_per_eur),
            prompt_cached_micro_per_m=(
                _microdollars_per_million(input_values[1], usd_per_eur)
                if len(input_values) > 1
                else None
            ),
        )
    return prices


def _load_prices() -> dict[str, ModelPrice]:
    return _parse_prices(
        fetch_html(PRICING_URL),
        usd_per_eur=_usd_per_eur(fetch_html(ECB_FX_URL)),
    )


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="SCALEWAY_SECRET_KEY",
        explicit_model_map=MODEL_MAP,
        expected_models=(
            "deepseek/deepseek-v4-flash-0731",
            "z-ai/glm-5.2",
            "qwen/qwen3.6-35b-a3b",
        ),
        pricing_source_url=PRICING_URL,
        price_loader=_load_prices,
        canary_max_tokens=32,
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
