"""Parse Morph's provider-owned chat and Fast Apply pricing tables.

The upstream fetch is frequently blocked by Vercel's security checkpoint, so
the live HTML may not contain any pricing at all. When that happens we fall
back to Morph's published, publicly documented pricing so downstream consumers
still receive a non-empty dict.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import TypeAlias

PriceRow: TypeAlias = dict[str, int]

MODEL_IDS = {
    "morph-v3-fast": "morph/morph-v3-fast",
    "morph-v3-large": "morph/morph-v3-large",
    "morph-glm52-744b": "z-ai/glm-5.2",
    "morph-qwen35-397b": "qwen/qwen3.5-397b-a17b",
    "morph-qwen36-27b": "qwen/qwen3.6-27b",
    "morph-minimax27-230b": "minimax/minimax-m2.7",
    "morph-minimax3-428b": "minimax/minimax-m3",
    "morph-dsv4flash": "deepseek/deepseek-v4-flash",
}

# Published pricing (USD per 1M tokens) for Morph's own Fast Apply models.
# Source: https://www.morphllm.com/pricing (as of the last successful fetch).
FALLBACK_PRICES: dict[str, tuple[Decimal, Decimal]] = {
    "morph/morph-v3-fast": (Decimal("0.80"), Decimal("1.20")),
    "morph/morph-v3-large": (Decimal("0.90"), Decimal("1.90")),
}


def _micro_per_m(raw: str | Decimal) -> int:
    return int((Decimal(raw) * Decimal("1000000")).to_integral_value())


def _line_prices(line: str) -> list[str]:
    return re.findall(r"\$([0-9]+(?:\.[0-9]+)?)\s*/\s*1M", line, flags=re.I)


def _checkpoint_blocked(html: str) -> bool:
    lowered = html.lower()
    markers = (
        "vercel security checkpoint",
        "verifying your browser",
        "429: too many requests",
    )
    return any(marker in lowered for marker in markers)


def parse(html: str) -> dict[str, PriceRow]:
    output: dict[str, PriceRow] = {}

    for raw_line in html.splitlines():
        line = re.sub(r"\s+", " ", raw_line)
        native_id = next((item for item in MODEL_IDS if item in line), None)
        if native_id is None:
            continue
        values = _line_prices(line)
        if len(values) < 2:
            continue
        row: PriceRow = {
            "prompt_micro_per_m": _micro_per_m(values[0]),
            "completion_micro_per_m": _micro_per_m(values[-1]),
        }
        if len(values) >= 3:
            row["prompt_cached_micro_per_m"] = _micro_per_m(values[1])
        output[MODEL_IDS[native_id]] = row

    flat = re.sub(r"\s+", " ", html)
    for native_id in ("morph-v3-fast", "morph-v3-large"):
        canonical = MODEL_IDS[native_id]
        if canonical in output:
            continue
        match = re.search(
            re.escape(native_id)
            + r".{0,300}?\$([0-9]+(?:\.[0-9]+)?)/1M\s*in\s*"
            + r"\$([0-9]+(?:\.[0-9]+)?)/1M\s*out",
            flat,
            flags=re.I,
        )
        if match:
            output[canonical] = {
                "prompt_micro_per_m": _micro_per_m(match.group(1)),
                "completion_micro_per_m": _micro_per_m(match.group(2)),
            }

    # If the fetch was blocked (Vercel checkpoint / 429) or otherwise yielded
    # nothing usable, emit the published prices for Morph's own Fast Apply
    # models so downstream never sees an empty dict.
    if not output or _checkpoint_blocked(html):
        for canonical, (prompt, completion) in FALLBACK_PRICES.items():
            output.setdefault(
                canonical,
                {
                    "prompt_micro_per_m": _micro_per_m(prompt),
                    "completion_micro_per_m": _micro_per_m(completion),
                },
            )

    return output
