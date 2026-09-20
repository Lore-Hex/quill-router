"""Vertex's standard global text-token rates, independent of AI Studio.

Keep this date-aware adapter outside the LLM-rewriteable parser sandbox.
Google publishes future rates beside current rates and separate tables for
priority, batch, audio, images and cache storage. None are interchangeable.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from decimal import Decimal

from bs4 import BeautifulSoup

from scripts.pricing.base import ModelPrice, PriceTier

_MODEL = re.compile(r"^Gemini (\d+(?:\.\d+)?) (Pro|Flash(?:[ -]Lite)?)( Preview| Cyber)?$", re.I)
_DATE = re.compile(r"\b(through|starting) ([A-Za-z]+ \d{1,2}, \d{4})\b", re.I)


def _model(label: str, today: date) -> str | None:
    period = _DATE.search(label)
    if period:
        boundary = datetime.strptime(period[2], "%B %d, %Y").date()
        if (period[1].lower() == "through" and today > boundary) or (
            period[1].lower() == "starting" and today < boundary
        ):
            return None
        label = label[: period.start()]
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


def parse(html: str, *, as_of: date | None = None) -> dict[str, ModelPrice]:
    today = as_of or datetime.now(UTC).date()
    parts: dict[str, dict[str, tuple[int, int, int | None, int | None]]] = {}
    for table in BeautifulSoup(html, "html.parser").find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        headers = [c.get_text(" ", strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        header = " ".join(headers)
        if headers[:2] != ["model", "type"] or "200k" not in header or "cached input" not in header:
            continue
        if any(word in header for word in ("priority", "flex", "batch")):
            continue
        regional = len(headers) > 2 and headers[2] == "region"
        offset = 3 if regional else 2
        if len(headers) != offset + 4:
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
            values = cells[offset:]
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
        if set(rates) != {"input", "output"}:
            # An incomplete SKU is not a price. The adapter records and holds
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
    if not prices:
        raise ValueError("vertex: no standard global Gemini text prices found")
    return prices
