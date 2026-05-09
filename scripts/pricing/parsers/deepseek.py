# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
# Initial version derived from a real fetch of
# api-docs.deepseek.com/quick_start/pricing on 2026-05-08.
# Captured fixture lives at tests/fixtures/pricing/deepseek.html.
#
# Page structure: a single <table> with model columns and price rows.
# Header row: <td>MODEL</td><td>deepseek-v4-flash</td><td>deepseek-v4-pro</td>
# Each price row has a label like "1M INPUT TOKENS (CACHE MISS)" with
# per-model values like "$0.14" / "$0.435 (75% off)<del>$1.74</del>".
# We use cache-miss input pricing (the headline rate, no cache discount)
# and the bare output token price.
"""DeepSeek pricing-page parser."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

# Native model id (as displayed in the column header) → OR-canonical id.
_NAME_TO_OR_ID = {
    "deepseek-v4-flash": "deepseek/deepseek-v4-flash",
    "deepseek-v4-pro": "deepseek/deepseek-v4-pro",
    "deepseek-chat": "deepseek/deepseek-chat",
    "deepseek-reasoner": "deepseek/deepseek-reasoner",
}

_DOLLAR_RE = re.compile(r"\$([\d.]+)")


def _to_micro_per_m(text: str | None) -> int | None:
    if not text:
        return None
    # The HTML for discounted prices looks like
    #   "$0.435 (75% off<sup>(3)</sup>)<del>$1.74</del>"
    # We want the FIRST $-number, which is the actual current price
    # (not the struck-through pre-discount value).
    match = _DOLLAR_RE.search(text)
    if not match:
        return None
    try:
        return int(round(float(match.group(1)) * 1_000_000))
    except (TypeError, ValueError):
        return None


_FOOTNOTE_RE = re.compile(r"\s*\(\d+\)\s*$")


def _strip_footnote(name: str) -> str:
    """DeepSeek annotates model names with footnote markers like
    `deepseek-v4-flash (1)`. Strip them before mapping to OR-canonical ids."""
    return _FOOTNOTE_RE.sub("", name).strip()


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        # Find the header row: cells[0] is the literal "MODEL", cells[1:]
        # are the per-model column headers.
        header_models: list[str] = []
        header_idx = -1
        for i, row in enumerate(rows):
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
            if cells and cells[0].upper() == "MODEL":
                header_models = [_strip_footnote(c) for c in cells[1:]]
                header_idx = i
                break
        if not header_models:
            continue
        # Find the cache-miss input row, the cache-hit (cached) row,
        # and the output row. DeepSeek's layout uses a `PRICING`
        # rowspan on the first pricing row, so the cache-miss row's
        # first cell is the label not the rowspan category — in either
        # case cells[0] (or cells[1]) holds the row label.
        input_prices: list[str] | None = None
        cached_prices: list[str] | None = None
        output_prices: list[str] | None = None
        for row in rows[header_idx + 1 :]:
            cells = [td.get_text(" ", strip=True) for td in row.find_all(["td", "th"])]
            if not cells:
                continue
            # Search across the first two cells for the label — handles
            # both rowspan-prefixed rows ([PRICING, INPUT TOKENS, ...])
            # and plain rows ([INPUT TOKENS, ...]).
            label_text = " ".join(cells[: min(2, len(cells))]).upper()
            # Prices are the last `len(header_models)` cells of the row,
            # regardless of how many label cells precede them.
            if len(cells) < len(header_models):
                continue
            value_cells = cells[-len(header_models) :]
            if "INPUT" in label_text and "CACHE MISS" in label_text:
                input_prices = value_cells
            elif "INPUT" in label_text and "CACHE HIT" in label_text:
                cached_prices = value_cells
            elif "OUTPUT" in label_text and "TOKEN" in label_text:
                output_prices = value_cells
        if input_prices is None or output_prices is None:
            continue
        for idx, native in enumerate(header_models):
            or_id = _NAME_TO_OR_ID.get(native)
            if or_id is None:
                continue
            if idx >= len(input_prices) or idx >= len(output_prices):
                continue
            prompt = _to_micro_per_m(input_prices[idx])
            completion = _to_micro_per_m(output_prices[idx])
            if prompt is None or completion is None:
                continue
            row_out: dict = {
                "prompt_micro_per_m": prompt,
                "completion_micro_per_m": completion,
            }
            if cached_prices is not None and idx < len(cached_prices):
                cached = _to_micro_per_m(cached_prices[idx])
                if cached is not None:
                    row_out["prompt_cached_micro_per_m"] = cached
            out[or_id] = row_out
    return out
