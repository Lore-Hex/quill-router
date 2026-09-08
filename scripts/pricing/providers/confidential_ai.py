"""Confidential.ai live catalog joined to its public token-price table."""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, fetch_html
from scripts.pricing.model_ids import canonicalize_unqualified_model_id
from scripts.pricing.providers._direct_openai import DirectOpenAIProvider, DirectOpenAIProviderSpec

SLUG = "confidential-ai"
BASE_URL = "https://api.confidential.ai/v1"
URL = f"{BASE_URL}/models"
PRICING_URL = "https://confidential.ai/pricing"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3]
    / "src/trusted_router/data/provider_models/confidential-ai.json"
)
MANIFEST_STALE_FALLBACK = True
EXPLICIT_MODEL_MAP = {"MiniMaxAI/MiniMax-M3-MXFP8": "minimax/minimax-m3"}


def _token_price(text: str) -> int:
    if re.fullmatch(r"\$[0-9]+(?:\.[0-9]{1,6})?", text) is None:
        raise RuntimeError(f"confidential-ai: invalid USD/M token price {text!r}")
    return int(Decimal(text[1:]) * 1_000_000)


def _published_prices(html: str) -> dict[str, ModelPrice]:
    headers_required = {
        "model",
        "input (per 1m tokens)",
        "input cached (per 1m tokens)",
        "output (per 1m tokens)",
    }
    prices: dict[str, ModelPrice] = {}
    for table in BeautifulSoup(html, "html.parser").find_all("table"):
        headers = [
            " ".join(c.get_text(" ", strip=True).casefold().split()) for c in table.find_all("th")
        ]
        if set(headers) != headers_required or len(headers) != len(headers_required):
            continue
        for tr in table.find_all("tr"):
            cells = tr.find_all("td")
            if not cells:
                continue
            if len(cells) != len(headers):
                raise RuntimeError("confidential-ai: malformed token-price row")
            row = dict(zip(headers, (c.get_text(" ", strip=True) for c in cells), strict=True))
            model_id = canonicalize_unqualified_model_id("-".join(row["model"].split()))
            if model_id is None:
                raise RuntimeError(f"confidential-ai: unknown pricing model {row['model']!r}")
            cached = _token_price(row["input cached (per 1m tokens)"])
            price = ModelPrice(
                _token_price(row["input (per 1m tokens)"]),
                _token_price(row["output (per 1m tokens)"]),
                prompt_cached_micro_per_m=cached,
            )
            if (
                price.prompt_micro_per_m <= 0
                or price.completion_micro_per_m <= 0
                or cached > price.prompt_micro_per_m
            ):
                raise RuntimeError(f"confidential-ai: invalid token prices for {model_id}")
            if model_id in prices and prices[model_id] != price:
                raise RuntimeError(f"confidential-ai: conflicting token prices for {model_id}")
            prices[model_id] = price
    if not prices:
        raise RuntimeError("confidential-ai: token-price table missing or empty")
    # The API reference identifies this exact release under the public Flash
    # price. Do not silently apply its rate to future dated releases.
    source = prices.get("deepseek/deepseek-v4-flash")
    target = "deepseek/deepseek-v4-flash-0731"
    if source is not None:
        prices.setdefault(target, source)
    return prices


def _load_prices() -> dict[str, ModelPrice]:
    return _published_prices(fetch_html(PRICING_URL))


CATALOG = DirectOpenAIProvider(
    DirectOpenAIProviderSpec(
        slug=SLUG,
        base_url=BASE_URL,
        api_key_env="CONFIDENTIAL_AI_API_KEY",
        explicit_model_map=EXPLICIT_MODEL_MAP,
        price_loader=_load_prices,
        pricing_source_url=PRICING_URL,
        reviewed_unpriced_model_ids=frozenset({"minimax/minimax-m3"}),
        canary_max_tokens=512,
        canary_expected_content="PONG",
        canary_extra_body={"temperature": 0},
    ),
    manifest_path=MANIFEST_PATH,
)
UPSTREAM_ID_MAP = CATALOG.upstream_id_map
fetch = CATALOG.fetch
write_provider_manifest = CATALOG.write_provider_manifest
