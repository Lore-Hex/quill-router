"""Parse Voyage AI's official Markdown pricing tables."""

from __future__ import annotations

import re
from decimal import Decimal


def _microdollars_per_million(value: str) -> int:
    return int((Decimal(value) * Decimal(1_000_000)).to_integral_value())


def _embedding_sections(markdown: str) -> list[str]:
    sections: list[str] = []
    for start, end in (
        (r"^# Text Embeddings\s*$", r"^# Multimodal Embeddings\s*$"),
        (r"^## Older models\s*$", r"^# Fine-tuned models\s*$"),
    ):
        match = re.search(start + r"(.*?)" + end, markdown, re.MULTILINE | re.DOTALL)
        if match is not None:
            sections.append(match.group(1))
    return sections


def parse(markdown: str) -> dict[str, dict[str, int]]:
    prices: dict[str, dict[str, int]] = {}
    for section in _embedding_sections(markdown):
        for line in section.splitlines():
            if not line.lstrip().startswith("|"):
                continue
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) < 3:
                continue
            million_price = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)", cells[2])
            if million_price is None:
                continue
            for native_id in re.findall(r"`([^`]+)`", cells[0]):
                if not native_id.startswith("voyage-"):
                    continue
                prices[f"voyage/{native_id}"] = {
                    "prompt_micro_per_m": _microdollars_per_million(
                        million_price.group(1)
                    ),
                    "completion_micro_per_m": 0,
                }
    return prices
