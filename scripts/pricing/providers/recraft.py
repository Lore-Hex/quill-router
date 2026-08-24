"""Recraft first-party image catalog and per-image pricing."""

from __future__ import annotations

import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, ProviderPricingResult, fetch_html, validate
from scripts.pricing.manifest import guard_fixed_output_prices, write_discovered_chat_manifest

SLUG = "recraft"
BASE_URL = "https://external.api.recraft.ai/v1"
URL = "https://www.recraft.ai/docs/api-reference/pricing"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/recraft.json"
)

MODELS = {
    "Recraft V4.1 Utility Pro": ("recraft/recraftv4_1_utility_pro", "recraftv4_1_utility_pro"),
    "Recraft V4.1 Pro": ("recraft/recraftv4_1_pro", "recraftv4_1_pro"),
    "Recraft V4.1 Utility": ("recraft/recraftv4_1_utility", "recraftv4_1_utility"),
    "Recraft V4.1": ("recraft/recraftv4_1", "recraftv4_1"),
    "Recraft V4 Pro": ("recraft/recraftv4_pro", "recraftv4_pro"),
    "Recraft V4": ("recraft/recraftv4", "recraftv4"),
    "Recraft V3": ("recraft/recraftv3", "recraftv3"),
    "Recraft V2": ("recraft/recraftv2", "recraftv2"),
}
PRO_MODELS = {
    "recraft/recraftv4_1_utility_pro",
    "recraft/recraftv4_1_pro",
    "recraft/recraftv4_pro",
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
        description = cells[0].get_text("\n", strip=True)
        if "Raster image generation" not in description:
            continue
        match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", cells[1].get_text(" ", strip=True))
        if match is None:
            continue
        price = _microdollars(match.group(1))
        for name in MODELS:
            # Recraft groups multiple models into shared rows. Requiring a
            # line/end boundary captures every model in the row without
            # treating "Recraft V4.1" as a prefix match for its Pro/Utility
            # siblings.
            if re.search(rf"{re.escape(name)}(?=$|\n)", description):
                model_id, _ = MODELS[name]
                prices[model_id] = price
    return prices


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603
    key = os.environ.get("RECRAFT_API_KEY", "").strip()
    if not key:
        raise RuntimeError("recraft: RECRAFT_API_KEY is required")
    html = fetch_html(URL)
    fixed_prices = _parse_pricing(html)
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        response = client.get(f"{BASE_URL}/users/me", headers={"Authorization": f"Bearer {key}"})
        response.raise_for_status()
    rows: dict[str, dict[str, Any]] = {}
    for name, (model_id, upstream_id) in MODELS.items():
        fixed = fixed_prices.get(model_id)
        if fixed is None:
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
            "fixed_output_price_microdollars": {"2k" if model_id in PRO_MODELS else "1k": fixed},
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
        fetched_url=URL,
        include_in_price_index=INCLUDE_IN_PRICE_INDEX,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_ROWS,
        source_url=URL,
        pricing_source_url=URL,
    )
