"""Parse the official Tinker models and pricing table."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from bs4 import BeautifulSoup, Tag

_INKLING_256K_ID = "thinkingmachines/Inkling:peft:262144"
_CANONICAL_ID = "thinkingmachines/inkling"


def _microdollars_per_million(text: str) -> int:
    match = re.search(r"\$([0-9]+(?:\.[0-9]+)?)", text)
    if match is None:
        raise ValueError(f"missing dollar price in {text!r}")
    return int((Decimal(match.group(1)) * Decimal("1000000")).to_integral_value(ROUND_HALF_UP))


def _price_span(cell: Tag, mode: str) -> Tag:
    span = cell.select_one(f".price-{mode}") or cell.select_one(".price-old")
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

    for row in soup.select("#model-tbody tr"):
        model_cell = row.select_one("td.tinker-id")
        if model_cell is None or model_cell.get_text(strip=True) != _INKLING_256K_ID:
            continue
        cells = row.find_all("td", recursive=False)
        if len(cells) < 8:
            continue
        input_span = _price_span(cells[6], mode)
        output_span = _price_span(cells[7], mode)
        cache_span = input_span.select_one(".price-cached")
        if cache_span is None:
            raise ValueError("Inkling 256K pricing row has no cached-input rate")
        return {
            _CANONICAL_ID: {
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
