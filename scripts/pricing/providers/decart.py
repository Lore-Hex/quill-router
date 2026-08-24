"""Decart authenticated media catalog and official fixed pricing."""

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

SLUG = "decart"
BASE_URL = "https://api.decart.ai"
URL = "https://docs.platform.decart.ai/getting-started/pricing"
MANIFEST_PATH = (
    Path(__file__).resolve().parents[3] / "src/trusted_router/data/provider_models/decart.json"
)
IMAGE_MODEL_ID = "decart/lucy-image-2"
IMAGE_UPSTREAM_ID = "lucy-image-2"
VIDEO_MODELS: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "decart/lucy-2.5": (
        "lucy-2.5",
        "Lucy 2.5",
        ("video-editing", "reference-images"),
    ),
    "decart/lucy-vton-3.5": (
        "lucy-vton-3.5",
        "Lucy VTON 3.5",
        ("video-editing", "virtual-try-on", "reference-images"),
    ),
    "decart/lucy-restyle-2": (
        "lucy-restyle-2",
        "Lucy Restyle 2",
        ("video-editing", "video-restyling", "reference-images"),
    ),
}
_DISCOVERED_ROWS: dict[str, dict[str, Any]] = {}
INCLUDE_IN_PRICE_INDEX = False
MANIFEST_STALE_FALLBACK = True


def _microdollars(raw: str) -> int:
    return int(Decimal(raw) * Decimal(1_000_000))


def _parse_price_cell(cell: Any) -> int:
    match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", cell.get_text(" ", strip=True))
    if match is None:
        raise RuntimeError("decart: pricing row is malformed")
    return _microdollars(match.group(1))


def _parse_pricing(html: str) -> dict[str, int | dict[str, int]]:
    soup = BeautifulSoup(html, "html.parser")
    prices: dict[str, int | dict[str, int]] = {}
    wanted_video_upstream_ids = {spec[0] for spec in VIDEO_MODELS.values()}
    for row in soup.select("tr"):
        cells = row.find_all(["td", "th"])
        if len(cells) < 4:
            continue
        upstream_id = cells[1].get_text(" ", strip=True).strip("`")
        if upstream_id == IMAGE_UPSTREAM_ID and len(cells) >= 5:
            prices[IMAGE_MODEL_ID] = {
                "480p": _parse_price_cell(cells[2]),
                "720p": _parse_price_cell(cells[3]),
            }
        elif upstream_id in wanted_video_upstream_ids and len(cells) >= 5:
            # The four-column realtime table has no 480p column. Requiring the
            # five-column async table prevents the cheaper realtime rate from
            # ever being applied to queued video jobs.
            prices[f"decart/{upstream_id}"] = _parse_price_cell(cells[3])
    expected = {IMAGE_MODEL_ID, *VIDEO_MODELS}
    missing = sorted(expected - prices.keys())
    if missing:
        raise RuntimeError(f"decart: official pricing rows missing: {', '.join(missing)}")
    return prices


def fetch() -> ProviderPricingResult:
    global _DISCOVERED_ROWS  # noqa: PLW0603
    key = os.environ.get("DECART_API_KEY", "").strip()
    if not key:
        raise RuntimeError("decart: DECART_API_KEY is required")
    fixed_prices = _parse_pricing(fetch_html(URL))
    upstream_ids = [IMAGE_UPSTREAM_ID, *(spec[0] for spec in VIDEO_MODELS.values())]
    with httpx.Client(timeout=20, follow_redirects=False) as client:
        for upstream_id in upstream_ids:
            response = client.post(
                f"{BASE_URL}/v1/models/resolve",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"model": upstream_id},
            )
            response.raise_for_status()
            if upstream_id not in str(response.json()):
                raise RuntimeError(
                    f"decart: authenticated model resolver did not return {upstream_id}"
                )
    _DISCOVERED_ROWS = {
        IMAGE_MODEL_ID: {
            "id": IMAGE_MODEL_ID,
            "upstream_id": IMAGE_UPSTREAM_ID,
            "display_name": "Lucy Image 2",
            "model_type": "image",
            "input_modalities": ["text", "image"],
            "output_modalities": ["image"],
            "endpoints": ["images"],
            "supported_features": ["image-editing"],
            "fixed_output_price_microdollars": fixed_prices[IMAGE_MODEL_ID],
            "routable": True,
            "status": 1,
        }
    }
    for model_id, (upstream_id, display_name, features) in VIDEO_MODELS.items():
        _DISCOVERED_ROWS[model_id] = {
            "id": model_id,
            "upstream_id": upstream_id,
            "display_name": display_name,
            "model_type": "video",
            "input_modalities": ["text", "image", "video"],
            "output_modalities": ["video"],
            "endpoints": ["videos"],
            "supported_features": list(features),
            "fixed_output_price_per_second_microdollars": fixed_prices[model_id],
            "routable": True,
            "status": 1,
        }
    guard_fixed_output_prices(MANIFEST_PATH, _DISCOVERED_ROWS)
    prices = {model_id: ModelPrice(0, 0) for model_id in _DISCOVERED_ROWS}
    errors = validate(prices, _DISCOVERED_ROWS, allow_all_zero=True)
    if errors:
        raise RuntimeError("; ".join(errors))
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
        source_url=f"{BASE_URL}/v1/models/resolve",
        pricing_source_url=URL,
    )
