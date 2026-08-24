"""Parse the official Tinker models and pricing table."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from bs4 import BeautifulSoup, Tag

_SERVERLESS_IDS = {
    "thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4": (
        "thinkingmachines/inkling-small"
    ),
    "thinkingmachines/Inkling:peft:262144:sampling-nvfp4": (
        "thinkingmachines/inkling"
    ),
}
_LEGACY_INKLING_256K_ID = "thinkingmachines/Inkling:peft:262144"
_LEGACY_CANONICAL_ID = "thinkingmachines/inkling"


def _microdollars_per_million(text: str) -> int:
    match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", text)
    if match is None:
        raise ValueError(f"missing dollar price in {text!r}")
    return int((Decimal(match.group(1)) * Decimal("1000000")).to_integral_value(ROUND_HALF_UP))


def _price_span(cell: Tag, mode: str) -> Tag:
    # Discounted rows use ``<s class=price-original>`` for the crossed-out
    # list price and ``.price-current`` for the amount actually charged.
    # Prefer the explicit current value before consulting the legacy toggle
    # classes; otherwise parsing the whole cell picks the struck-through
    # amount first and creates a false 2x price spike.
    span = (
        cell.select_one(".price-current")
        or cell.select_one(f".price-{mode}")
        or cell.select_one(".price-old")
    )
    if isinstance(span, Tag):
        return span
    # The current server-rendered table puts the active dollar price directly
    # on ``td.price`` and reserves a nested span only for the cached rate.
    if "price" in {str(value) for value in (cell.get("class") or [])}:
        return cell
    raise ValueError("missing active price span")


def parse(html: str) -> dict[str, dict[str, int]]:
    soup = BeautifulSoup(html, "html.parser")
    active = soup.select_one("#pricing-toggle button.active")
    mode = str(active.get("data-mode")) if isinstance(active, Tag) else "old"

    prices: dict[str, dict[str, int]] = {}
    for row in soup.select("#serverless-tbody tr"):
        model_cell = row.select_one("td.tinker-id")
        if model_cell is None:
            continue
        canonical_id = _SERVERLESS_IDS.get(model_cell.get_text(strip=True))
        if canonical_id is None:
            continue
        cells = row.find_all("td", recursive=False)
        if len(cells) < 5:
            continue
        input_span = _price_span(cells[3], mode)
        output_span = _price_span(cells[4], mode)
        cache_span = input_span.select_one(".price-cached")
        if cache_span is None:
            cache_span = cells[3].select_one(".price-cached")
        if cache_span is None:
            raise ValueError(f"{canonical_id} serverless row has no cached-input rate")
        prices[canonical_id] = {
            "prompt_micro_per_m": _microdollars_per_million(
                input_span.get_text(" ", strip=True)
            ),
            "completion_micro_per_m": _microdollars_per_million(
                output_span.get_text(" ", strip=True)
            ),
            "prompt_cached_micro_per_m": _microdollars_per_million(
                cache_span.get_text(" ", strip=True)
            ),
        }
    if prices:
        return prices

    for row in soup.select("#model-tbody tr"):
        model_cell = row.select_one("td.tinker-id")
        if (
            model_cell is None
            or model_cell.get_text(strip=True) != _LEGACY_INKLING_256K_ID
        ):
            continue
        cells = row.find_all("td", recursive=False)
        if len(cells) < 8:
            continue
        input_span = _price_span(cells[6], mode)
        output_span = _price_span(cells[7], mode)
        cache_span = input_span.select_one(".price-cached") or cells[6].select_one(
            ".price-cached"
        )
        if cache_span is None:
            raise ValueError("Inkling 256K pricing row has no cached-input rate")
        return {
            _LEGACY_CANONICAL_ID: {
                "prompt_micro_per_m": _microdollars_per_million(
                    input_span.get_text(" ", strip=True)
                ),
                "completion_micro_per_m": _microdollars_per_million(
                    output_span.get_text(" ", strip=True)
                ),
                "prompt_cached_micro_per_m": _microdollars_per_million(
                    cache_span.get_text(" ", strip=True)
                ),
            }
        }
    return {}
