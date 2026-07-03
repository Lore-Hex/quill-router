"""Parser for Xiaomi MiMo API pricing."""

from __future__ import annotations

import re


def _money_to_micro_per_m(value: str) -> int:
    return int(round(float(value) * 1_000_000))


def _section(text: str, title: str) -> str:
    pattern = rf"####\s*{re.escape(title)}\s+(.*?)(?=####\s*MiMo-|##\s|$)"
    match = re.search(pattern, text, re.S)
    return match.group(1) if match else ""


def parse(html: str) -> dict[str, dict[str, int]]:
    text = re.sub(r"\s+", " ", html)
    prices: dict[str, dict[str, int]] = {}
    mapping = {
        "MiMo-V2.5-Pro": "xiaomi/mimo-v2.5-pro",
        "MiMo-V2.5-Pro-UltraSpeed": "xiaomi/mimo-v2.5-pro-ultraspeed",
        "MiMo-V2.5": "xiaomi/mimo-v2.5",
    }
    for title, model_id in mapping.items():
        block = _section(text, title)
        if not block:
            continue
        cache = re.search(r"Input \(cache hit\)\$([0-9.]+)\s*/\s*MTok", block)
        prompt = re.search(r"Input \(cache miss\)\$([0-9.]+)\s*/\s*MTok", block)
        completion = re.search(r"Output\$([0-9.]+)\s*/\s*MTok", block)
        if not prompt or not completion:
            continue
        row = {
            "prompt_micro_per_m": _money_to_micro_per_m(prompt.group(1)),
            "completion_micro_per_m": _money_to_micro_per_m(completion.group(1)),
        }
        if cache:
            row["prompt_cached_micro_per_m"] = _money_to_micro_per_m(cache.group(1))
        prices[model_id] = row
    return prices
