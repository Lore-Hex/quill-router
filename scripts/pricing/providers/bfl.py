"""Black Forest Labs image endpoints and official per-image prices."""

from __future__ import annotations

import json
import os
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_html, validate
from scripts.pricing.manifest import guard_fixed_output_prices, write_discovered_chat_manifest

SLUG = "bfl"
OPENAPI_URL = "https://api.bfl.ai/openapi.json"
PRICING_URL = "https://docs.bfl.ml/quick_start/pricing"
CALCULATOR_URL = "https://bfl.ai/pricing"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/bfl.json"
)
MODELS = {
    "FLUX.2 [klein] 4B": ("black-forest-labs/flux-2-klein-4b", "flux-2-klein-4b"),
    "FLUX.2 [klein] 9B": ("black-forest-labs/flux-2-klein-9b", "flux-2-klein-9b"),
    "FLUX.2 [pro]": ("black-forest-labs/flux-2-pro", "flux-2-pro"),
    "FLUX.2 [max]": ("black-forest-labs/flux-2-max", "flux-2-max"),
    "FLUX.2 [flex]": ("black-forest-labs/flux-2-flex", "flux-2-flex"),
}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}
INCLUDE_IN_PRICE_INDEX = False
MANIFEST_STALE_FALLBACK = True


def _microdollars(raw: str) -> int:
    return int(Decimal(raw) * Decimal(1_000_000))


def _parse_pricing(html: str) -> dict[str, int]:
    soup = BeautifulSoup(html, "html.parser")
    prices: dict[str, int] = {}
    for row in soup.select("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 2:
            continue
        name = cells[0].get_text(" ", strip=True)
        mapped = MODELS.get(name)
        match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", cells[1].get_text(" ", strip=True))
        if mapped is not None and match is not None:
            prices[mapped[0]] = _microdollars(match.group(1))
    return prices


def _parse_calculator_pricing(html: str) -> dict[str, int]:
    """Read BFL's server-rendered calculator data without executing JavaScript.

    firstMpPrice is in credits (one cent), not dollars or the subsequent-MP
    rate. Only our existing text-to-image endpoints and 1K price are imported.
    """
    endpoints = {f"/v1/{upstream}": model for model, upstream in MODELS.values()}
    prices: dict[str, int] = {}
    decoder = json.JSONDecoder()
    for script in BeautifulSoup(html, "html.parser").find_all("script"):
        match = re.fullmatch(r"self\.__next_f\.push\((.*)\);?", script.get_text().strip(), re.DOTALL)
        if match is None:
            continue
        try:
            call = json.loads(match.group(1))
            if not isinstance(call, list) or len(call) != 2 or not isinstance(call[1], str):
                continue
            value, _ = decoder.raw_decode(call[1].split(":", 1)[1].lstrip())
        except (ValueError, IndexError):
            continue
        pending = [value]
        while pending:
            value = pending.pop()
            if isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, dict):
                pending.extend(value.values())
                cards = value.get("pricingCards")
                if not isinstance(cards, list):
                    continue
                for card in cards:
                    if not isinstance(card, dict):
                        continue
                    model = endpoints.get(str(card.get("apiEndpoint")))
                    if model is None:
                        continue
                    try:
                        amount = Decimal(str(card.get("firstMpPrice"))) * 10_000
                    except InvalidOperation as exc:
                        raise ValueError("BFL calculator has an invalid first-MP price") from exc
                    if not amount.is_finite() or amount <= 0 or amount != amount.to_integral_value():
                        raise ValueError("BFL calculator has an invalid first-MP price")
                    price = int(amount)
                    if model in prices and prices[model] != price:
                        raise ValueError("BFL calculator has conflicting first-MP prices")
                    prices[model] = price
    return prices


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603
    key = os.environ.get("BFL_API_KEY", "").strip()
    if not key:
        raise RuntimeError("bfl: BFL_API_KEY is required")
    html = fetch_html(PRICING_URL)
    fixed_prices = _parse_pricing(html)
    pricing_source = PRICING_URL
    if any(model_id not in fixed_prices for model_id, _ in MODELS.values()):
        fixed_prices = _parse_calculator_pricing(fetch_html(CALCULATOR_URL))
        pricing_source = CALCULATOR_URL
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        catalog = client.get(OPENAPI_URL)
        catalog.raise_for_status()
        paths = catalog.json().get("paths", {})
        credits = client.get("https://api.bfl.ai/v1/credits", headers={"x-key": key})
        credits.raise_for_status()
    rows: dict[str, dict[str, Any]] = {}
    for name, (model_id, upstream_id) in MODELS.items():
        fixed = fixed_prices.get(model_id)
        if fixed is None or f"/v1/{upstream_id}" not in paths:
            continue
        rows[model_id] = {
            "id": model_id,
            "upstream_id": upstream_id,
            "display_name": name,
            "model_type": "image",
            "input_modalities": ["text"],
            "output_modalities": ["image"],
            "endpoints": ["images"],
            "supported_features": ["image-generation"],
            "fixed_output_price_microdollars": {"1k": fixed},
            "routable": True,
            "status": 1,
        }
    guard_fixed_output_prices(MANIFEST_PATH, rows)
    prices = {model_id: ModelPrice(0, 0) for model_id in rows}
    errors = validate(prices, [model_id for model_id, _ in MODELS.values()], allow_all_zero=True)
    if errors:
        raise RuntimeError("; ".join(errors))
    _DISCOVERED_ROWS = rows
    return ProviderPricingResult(
        slug=SLUG,
        prices=prices,
        source="api",
        fetched_url=pricing_source,
        include_in_price_index=INCLUDE_IN_PRICE_INDEX,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_ROWS,
        source_url=OPENAPI_URL,
        pricing_source_url=result.fetched_url,
    )
