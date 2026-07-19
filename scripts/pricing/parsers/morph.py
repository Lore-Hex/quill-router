"""Parse Morph's provider-owned chat and Fast Apply pricing tables."""

from __future__ import annotations

import re
from decimal import Decimal

MODEL_IDS = {
    "morph-glm52-744b": "z-ai/glm-5.2",
    "morph-qwen35-397b": "qwen/qwen3.5-397b-a17b",
    "morph-qwen36-27b": "qwen/qwen3.6-27b",
    "morph-minimax27-230b": "minimax/minimax-m2.7",
    "morph-minimax3-428b": "minimax/minimax-m3",
    "morph-dsv4flash": "deepseek/deepseek-v4-flash",
    "morph-v3-fast": "morph/morph-v3-fast",
    "morph-v3-large": "morph/morph-v3-large",
}


def _micro_per_m(raw: str) -> int:
    return int((Decimal(raw) * Decimal("1000000")).to_integral_value())


def _line_prices(line: str) -> list[str]:
    return re.findall(r"\$([0-9]+(?:\.[0-9]+)?)\s*/\s*1M", line, flags=re.I)


def parse(html: str) -> dict[str, dict[str, int]]:
    output: dict[str, dict[str, int]] = {}
    for raw_line in html.splitlines():
        line = re.sub(r"\s+", " ", raw_line)
        native_id = next((item for item in MODEL_IDS if item in line), None)
        if native_id is None:
            continue
        values = _line_prices(line)
        if len(values) < 2:
            continue
        row = {
            "prompt_micro_per_m": _micro_per_m(values[0]),
            "completion_micro_per_m": _micro_per_m(values[-1]),
        }
        if len(values) >= 3:
            row["prompt_cached_micro_per_m"] = _micro_per_m(values[1])
        output[MODEL_IDS[native_id]] = row

    # Jina occasionally flattens the Fast Apply cards instead of emitting a
    # Markdown row. Parse their labelled "$X/1M in $Y/1M out" form too.
    flat = re.sub(r"\s+", " ", html)
    for native_id in ("morph-v3-fast", "morph-v3-large"):
        if MODEL_IDS[native_id] in output:
            continue
        match = re.search(
            rf"{re.escape(native_id)}.{{0,300}}?"
            r"\$([0-9]+(?:\.[0-9]+)?)/1M\s*in\s*"
            r"\$([0-9]+(?:\.[0-9]+)?)/1M\s*out",
            flat,
            flags=re.I,
        )
        if match:
            output[MODEL_IDS[native_id]] = {
                "prompt_micro_per_m": _micro_per_m(match.group(1)),
                "completion_micro_per_m": _micro_per_m(match.group(2)),
            }
    return output
