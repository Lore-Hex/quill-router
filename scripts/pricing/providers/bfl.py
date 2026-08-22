"""Black Forest Labs image endpoints and official per-image prices."""

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

SLUG = "bfl"
OPENAPI_URL = "https://api.bfl.ai/openapi.json"
PRICING_URL = "https://docs.bfl.ml/quick_start/pricing"
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


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603
    key = os.environ.get("BFL_API_KEY", "").strip()
    if not key:
        raise RuntimeError("bfl: BFL_API_KEY is required")
    html = fetch_html(PRICING_URL)
    fixed_prices = _parse_pricing(html)
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
        fetched_url=PRICING_URL,
        include_in_price_index=INCLUDE_IN_PRICE_INDEX,
    )


def write_provider_manifest(result: ProviderPricingResult) -> list[str]:
    return write_discovered_chat_manifest(
        result,
        manifest_path=MANIFEST_PATH,
        discovered_rows=_DISCOVERED_ROWS,
        source_url=OPENAPI_URL,
        pricing_source_url=PRICING_URL,
    )
