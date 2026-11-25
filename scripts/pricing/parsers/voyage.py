"""Parse Voyage AI's official Markdown pricing tables."""

from __future__ import annotations

import re
from decimal import Decimal


def _microdollars_per_million(value: str) -> int:
    return int((Decimal(value) * Decimal(1_000_000)).to_integral_value())


def parse(markdown: str) -> dict[str, dict[str, int]]:
    prices: dict[str, dict[str, int]] = {}
    section = ""
    million_column = None
    for line in markdown.splitlines():
        if line.startswith("#"):
            section = line.lstrip("#").strip().casefold()
            million_column = None
        if section not in {"text embeddings", "older models", "rerank", "rerankers", "reranking"}:
            continue
        if not line.lstrip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if cells[0].casefold() == "model":
            million_column = next((index for index, cell in enumerate(cells)
                                   if cell.casefold() == "price per million tokens"), None)
            continue
        if million_column is None or len(cells) <= million_column:
            continue
        million_price = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)", cells[million_column])
        if million_price is None:
            continue
        cost = _microdollars_per_million(million_price.group(1))
        if cost <= 0:
            continue
        for native_id in re.findall(r"`([^`]+)`", cells[0]):
            if not native_id.startswith(("voyage-", "rerank-")):
                continue
            prices[f"voyage/{native_id}"] = {
                "prompt_micro_per_m": cost,
                "completion_micro_per_m": 0,
            }
    return prices
