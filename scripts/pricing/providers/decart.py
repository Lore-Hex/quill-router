"""Decart authenticated media catalog and official fixed pricing."""

from __future__ import annotations

import os
import re
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


def _microdollars(dollars: str, cents_and_below: str | None) -> int:
    """Exact microdollars from a price's digits; a finer price is not billable."""
    fraction = (cents_and_below or "").rstrip("0")
    if len(fraction) > 6:
        raise RuntimeError(
            f"decart: ${dollars}.{cents_and_below} is not a whole number of microdollars"
        )
    return int(dollars) * 1_000_000 + int(fraction.ljust(6, "0"))


# Queued video jobs are billed per second and images per image. A realtime
# rate must never price a queued job. So a row is read only under a Video or
# Image models heading, only from a table in the queued and image shape (one
# ID, 480p and 720p column; realtime tables have no 480p), and by column
# header, never by position.
_SECTIONS = ("video models", "image models")
_SHAPE = ("id", "480p", "720p")
_PER_SECOND_PRICE = re.compile(r"\$([0-9]+)(?:\.([0-9]+))?/sec")
_PER_IMAGE_PRICE = re.compile(r"\$([0-9]+)(?:\.([0-9]+))?")


def _text(node: Any) -> str:
    return " ".join(node.get_text(" ", strip=True).replace("\u200b", "").split())


def _parse_price_cell(cell: Any, pattern: re.Pattern[str], where: str) -> int:
    text = _text(cell)
    match = pattern.fullmatch(text)
    if match is None:
        raise RuntimeError(f"decart: {where} is not a price: {text!r}")
    return _microdollars(match.group(1), match.group(2))


def _section(table: Any) -> str:
    heading = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
    return _text(heading).casefold() if heading is not None else ""


def _wanted(section: str, upstream_id: str) -> tuple[str, dict[str, re.Pattern[str]]] | None:
    """The model a row prices and the columns that price it, if any."""
    if section == "video models" and upstream_id in {spec[0] for spec in VIDEO_MODELS.values()}:
        return f"decart/{upstream_id}", {"720p": _PER_SECOND_PRICE}
    if section == "image models" and upstream_id == IMAGE_UPSTREAM_ID:
        return IMAGE_MODEL_ID, {"480p": _PER_IMAGE_PRICE, "720p": _PER_IMAGE_PRICE}
    return None


def _parse_pricing(html: str) -> dict[str, int | dict[str, int]]:
    soup = BeautifulSoup(html, "html.parser")
    prices: dict[str, int | dict[str, int]] = {}
    for table in soup.find_all("table"):
        section = _section(table)
        rows = table.find_all("tr")
        if section not in _SECTIONS or not rows:
            continue
        header = rows[0].find_all(["th", "td"])
        columns = [_text(cell).casefold() for cell in header]
        if columns.count("id") != 1:
            raise RuntimeError(f"decart: a {section} table needs exactly one ID column")
        for row in rows[1:]:
            cells = row.find_all(["td", "th"])
            if len(cells) != len(columns):
                raise RuntimeError(f"decart: a {section} row does not match its header")
            wanted = _wanted(section, _text(cells[columns.index("id")]).strip("`"))
            if wanted is None:
                continue
            model_id, priced_columns = wanted
            for name in _SHAPE:
                if columns.count(name) != 1:
                    raise RuntimeError(f"decart: {model_id} needs exactly one {name} column")
            if any(
                cell.get(span, "1") != "1"
                for cell in (*header, *cells)
                for span in ("colspan", "rowspan")
            ):
                raise RuntimeError(f"decart: {model_id}'s table spans cells")
            parsed = {
                name: _parse_price_cell(cells[columns.index(name)], pattern, f"{model_id} {name}")
                for name, pattern in priced_columns.items()
            }
            price: int | dict[str, int] = parsed if model_id == IMAGE_MODEL_ID else parsed["720p"]
            if prices.get(model_id, price) != price:
                raise RuntimeError(f"decart: {model_id} is listed at two prices")
            prices[model_id] = price
    expected = {IMAGE_MODEL_ID, *VIDEO_MODELS}
    missing_rows = sorted(expected - prices.keys())
    if missing_rows:
        raise RuntimeError(f"decart: official pricing rows missing: {', '.join(missing_rows)}")
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
