"""Parser for Xiaomi MiMo API pricing."""

from __future__ import annotations

import re
from decimal import Decimal


def _money_to_micro_per_m(value: str) -> int:
    return int((Decimal(value) * Decimal(1_000_000)).to_integral_value())


def _section(text: str, title: str) -> str:
    pattern = rf"####\s*{re.escape(title)}\s+(.*?)(?=####\s*MiMo-|##\s|$)"
    match = re.search(pattern, text, re.S)
    return match.group(1) if match else ""


def _overseas_payg_prices(html: str) -> dict[str, dict[str, int]]:
    """Parse the authoritative USD table from Xiaomi's PAYG page."""
    section_match = re.search(
        r"###\s*Overseas Pricing of the Model\s+(.*?)(?=###\s|$)",
        html,
        flags=re.I | re.S,
    )
    if not section_match:
        return {}

    mapping = {
        "mimo-v2.5-pro": "xiaomi/mimo-v2.5-pro",
        "mimo-v2.5": "xiaomi/mimo-v2.5",
    }
    prices: dict[str, dict[str, int]] = {}
    row_pattern = re.compile(
        r"\|\s*`?(mimo-v2\.5(?:-pro)?)`?\s*"
        r"\|\s*\$([0-9.]+)\s*"
        r"\|\s*\$([0-9.]+)\s*"
        r"\|\s*\$([0-9.]+)\s*\|",
        flags=re.I,
    )
    for model, cache, prompt, completion in row_pattern.findall(section_match.group(1)):
        model_id = mapping.get(model.casefold())
        if model_id is None:
            continue
        prices[model_id] = {
            "prompt_micro_per_m": _money_to_micro_per_m(prompt),
            "completion_micro_per_m": _money_to_micro_per_m(completion),
            "prompt_cached_micro_per_m": _money_to_micro_per_m(cache),
        }
    return prices


def parse(html: str) -> dict[str, dict[str, int]]:
    # The current official page publishes a Markdown table. Keep the
    # card parser below as a compatibility fallback for older captures and
    # for UltraSpeed if Xiaomi republishes its standalone PAYG card.
    prices = _overseas_payg_prices(html)
    text = re.sub(r"\s+", " ", html)
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
        prices.setdefault(model_id, row)
    return prices
