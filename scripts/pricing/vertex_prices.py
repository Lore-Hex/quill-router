"""Vertex's standard global text-token rates, independent of AI Studio.

Keep this date-aware adapter outside the LLM-rewriteable parser sandbox.
Google publishes future rates beside current rates and separate tables for
priority, batch, audio, images and cache storage. None are interchangeable.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal

from bs4 import BeautifulSoup, Tag

from scripts.pricing.base import ModelPrice, PriceTier

_MODEL = re.compile(
    r"^Gemini (\d+(?:\.\d+)?) (Pro|Flash(?:[ -]Lite)?|Argon|Carbon)( Preview| Cyber)?$", re.I
)
_DATE = re.compile(r"\b(through|starting) ([A-Za-z]+ \d{1,2}, \d{4})\b", re.I)


def _model(label: str, today: date) -> str | None:
    period = _DATE.search(label)
    if period:
        boundary = datetime.strptime(period[2], "%B %d, %Y").date()
        if (period[1].lower() == "through" and today > boundary) or (
            period[1].lower() == "starting" and today < boundary
        ):
            return None
        label = label[: period.start()].rstrip(" (")
    match = _MODEL.fullmatch(label.replace("*", "").strip())
    if match is None:
        return None
    suffix = (match[3] or "").strip().lower()
    variant = match[2].lower().replace(" ", "-")
    return f"google/gemini-{match[1]}-{variant}" + (f"-{suffix}" if suffix else "")


def _money(value: str) -> int:
    if not re.fullmatch(r"\$\d+(?:\.\d+)?", value):
        raise ValueError(f"vertex: expected one USD token price, got {value!r}")
    return int(Decimal(value[1:]) * 1_000_000)


def _table_tab(table: Tag, soup: BeautifulSoup) -> str | None:
    """Resolve the actual tab label; neither table order nor active state is a tariff."""
    panel = table.find_parent(attrs={"role": "tabpanel"})
    if panel is None:
        return None
    label_id = panel.get("aria-labelledby")
    label = soup.find(id=label_id) if isinstance(label_id, str) else None
    if label is None:
        raise ValueError("vertex: pricing tab has no label")
    return label.get_text(" ", strip=True).lower()


def parse(html: str, *, as_of: date | None = None) -> dict[str, ModelPrice]:
    prices = _scan(html, as_of=as_of)[0]
    if not prices:
        raise ValueError("vertex: no standard global Gemini text prices found")
    return prices


def cache_write_held(html: str, *, as_of: date | None = None) -> frozenset[str]:
    """Models withheld only because they publish a cache-write tariff we cannot bill."""
    return _scan(html, as_of=as_of)[1]


def _scan(
    html: str, *, as_of: date | None = None
) -> tuple[dict[str, ModelPrice], frozenset[str]]:
    today = as_of or datetime.now(UTC).date()
    parts: dict[str, dict[str, tuple[int, int, int | None, int | None]]] = {}
    unbillable: set[str] = set()
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        header = " ".join(headers)
        # Google calls the cache-hit column "Cached input" on older tables and
        # "Cache read" on Gemini 4 tables (renamed 2026-11-12 with Hydrogen).
        if headers[:2] != ["model", "type"] or not (
            "cached input" in header or "cache read" in header
        ):
            continue
        tab = _table_tab(table, soup)
        if tab is not None and tab not in {"standard", "standard model"}:
            continue
        if any(word in header for word in ("priority", "flex", "batch")):
            continue
        regional = len(headers) > 2 and headers[2] == "region"
        offset = 3 if regional else 2
        # A trailing "Cache write" column carries a third input tariff that
        # ModelPrice cannot bill. Strip it from the column contract and hold
        # any model whose input row publishes a real cache-write price.
        writes = [i for i, h in enumerate(headers) if "cache write" in h]
        if writes and writes != [len(headers) - 1]:
            raise ValueError("vertex: cache write column moved")
        width = len(headers) - len(writes)
        tiered = "200k" in header
        # Flat tables must positively identify the tariff. Identical headers
        # are used in the standard, priority and batch tabs.
        if not tiered and tab not in {"standard", "standard model"}:
            continue
        if width != offset + (4 if tiered else 2):
            raise ValueError("vertex: standard token table columns changed")
        model_id: str | None = None
        kind = ""
        for row in rows[1:]:
            cells = [
                c.get_text(" ", strip=True) for c in row.find_all(["th", "td"], recursive=False)
            ]
            if len(cells) != len(headers):
                raise ValueError("vertex: standard token row columns changed")
            if cells[0]:
                model_id = _model(cells[0], today)
                kind = ""
            if cells[1]:
                kind = cells[1].lower()
            if model_id is None or (regional and cells[2].lower() != "global"):
                continue
            direction = (
                "input"
                if kind.startswith("input (text")
                else "output"
                if kind.startswith("text output")
                else None
            )
            if direction is None:
                continue
            values = cells[offset:width]
            if direction == "input" and writes and cells[writes[0]] != "N/A":
                unbillable.add(model_id)
            if not tiered:
                values = [values[0], values[0], values[1], values[1]]
            rate = (
                _money(values[0]),
                _money(values[1]),
                _money(values[2]) if direction == "input" and values[2] != "N/A" else None,
                _money(values[3]) if direction == "input" and values[3] != "N/A" else None,
            )
            previous = parts.setdefault(model_id, {}).get(direction)
            if previous is not None and previous != rate:
                raise ValueError(f"vertex: conflicting current {direction} rates for {model_id}")
            parts[model_id][direction] = rate
    prices: dict[str, ModelPrice] = {}
    for model_id, rates in parts.items():
        if set(rates) != {"input", "output"} or model_id in unbillable:
            # An incomplete SKU is not a price, and neither is one with a
            # cache-write tariff we cannot bill. The adapter records and holds
            # that route without discarding other fully priced models.
            continue
        inp, out = rates["input"], rates["output"]
        low, high = (inp[0], out[0], inp[2]), (inp[1], out[1], inp[3])
        if low == high:
            prices[model_id] = ModelPrice(low[0], low[1], prompt_cached_micro_per_m=low[2])
        else:
            prices[model_id] = ModelPrice(
                tiers=[
                    PriceTier(200_000, *low),
                    PriceTier(None, *high),
                ]
            )
    return prices, frozenset(unbillable)
