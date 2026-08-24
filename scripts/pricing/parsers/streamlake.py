"""Parse StreamLake Vanchin's official pay-as-you-go KAT pricing table."""

from __future__ import annotations

import re
from decimal import Decimal

MODEL_IDS = {
    "KAT-Coder-Pro-V2.5": "kwaipilot/kat-coder-pro-v2.5",
    "KAT-Coder-Air-V2.5": "kwaipilot/kat-coder-air-v2.5",
    "KAT-Coder-Pro-V2": "kwaipilot/kat-coder-pro-v2",
}


def _micro_per_m(raw: str) -> int:
    return int((Decimal(raw) * Decimal("1000000")).to_integral_value())


def _pricing_rows(raw: str) -> list[str]:
    # The live Next.js document serializes the entire article as one JSON
    # string with escaped HTML tags. Isolate table rows before looking for
    # prices so unrelated package prices cannot leak into a model row.
    decoded = (
        raw.replace("\\u003c", "<")
        .replace("\\u003e", ">")
        .replace("\\u0026", "&")
    )
    table_rows = re.findall(r"<tr\b[^>]*>(.*?)</tr>", decoded, re.IGNORECASE | re.DOTALL)
    if not table_rows:
        return raw.splitlines()
    return [re.sub(r"<[^>]+>", " ", table_row) for table_row in table_rows]


def parse(html: str) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for raw_line in _pricing_rows(html):
        line = re.sub(r"\s+", " ", raw_line)
        label = next((item for item in MODEL_IDS if item.casefold() in line.casefold()), None)
        if label is None:
            continue
        values = re.findall(r"\$([0-9]+(?:\.[0-9]+)?)", line)
        if len(values) < 2:
            continue
        row = {
            "prompt_micro_per_m": _micro_per_m(values[0]),
            "completion_micro_per_m": _micro_per_m(values[1]),
        }
        if len(values) >= 3:
            row["prompt_cached_micro_per_m"] = _micro_per_m(values[-1])
        output[MODEL_IDS[label]] = row
    return output
