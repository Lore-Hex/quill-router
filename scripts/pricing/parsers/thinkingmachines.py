"""Parse the official Tinker models and pricing table."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal

from bs4 import BeautifulSoup, Tag

# Exact serverless Tinker IDs, keyed to the catalog model they price. Each key
# must equal the manifest row's upstream_id, so a price is only ever verified
# for the ID the route sends. Inkling and Inkling-Small keep the
# ":sampling-nvfp4" suffix. By 2026-11-25 serverless.json listed only these
# two, and the docs say serverless inference "is available for Inkling and
# Inkling-Small only"; Inkling 2 Small moved to the sampler table below.
_SERVERLESS_IDS = {
    "thinkingmachines/Inkling-Small:peft:262144:sampling-nvfp4": (
        "thinkingmachines/inkling-small"
    ),
    "thinkingmachines/Inkling:peft:262144:sampling-nvfp4": (
        "thinkingmachines/inkling"
    ),
}
# Exact sampler (models.json) Tinker IDs, priced at their paid sample rates.
# Inkling 2 Small keeps the upstream_id its route already sends
# ("thinkingmachines/Inkling-2-Small:peft:262144"): models.json lists that
# exact ID at the rates serverless.json carried ($0.30 / $0.03 / $1.00).
# Inkling 2 Mini is the paid 256K row. Its free row
# ("thinkingmachines/Inkling-2-Mini:peft:65536:free") is deliberately absent:
# it is rate-limited, 64K-only and needs a positive balance, so it is not a
# route TR can sell or price.
_SAMPLER_IDS = {
    "thinkingmachines/Inkling-2-Mini:peft:262144": "thinkingmachines/inkling-2-mini",
    "thinkingmachines/Inkling-2-Small:peft:262144": "thinkingmachines/inkling-2-small",
    "zai-org/GLM-5.3:peft:262144": "z-ai/glm-5.3",
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
    for row in soup.select("#model-tbody tr"):
        model_cell = row.select_one("td.tinker-id")
        if model_cell is None:
            continue
        canonical_id = _SAMPLER_IDS.get(model_cell.get_text(strip=True))
        if canonical_id is None:
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
            raise ValueError(f"{canonical_id} sampler row has no cached-input rate")
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
