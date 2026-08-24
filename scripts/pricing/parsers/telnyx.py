"""Parse Telnyx's current inference pricing page."""

from __future__ import annotations

import re
from decimal import Decimal

_MODEL_LABELS = {
    "Kimi K2.6": "moonshotai/kimi-k2.6",
    "GLM-5.2": "z-ai/glm-5.2",
    "MiniMax-M3": "minimax/minimax-m3",
}


def _money_to_micro(raw: str) -> int:
    return int((Decimal(raw) * Decimal(1_000_000)).to_integral_value())


def parse(html: str) -> dict[str, dict[str, int]]:
    text = re.sub(r"\s+", " ", html.replace(r"\$", "$"))
    prices: dict[str, dict[str, int]] = {}
    for label, model_id in _MODEL_LABELS.items():
        match = re.search(
            rf"{re.escape(label)}\b(?P<section>.{{0,500}}?)"
            r"Input:\s*\$([0-9]+(?:\.[0-9]+)?)\s*/\s*1M\s+tokens"
            r".{0,160}?Cached Input:\s*\$([0-9]+(?:\.[0-9]+)?)\s*/\s*1M\s+tokens"
            r".{0,160}?Output:\s*\$([0-9]+(?:\.[0-9]+)?)\s*/\s*1M\s+tokens",
            text,
            flags=re.I,
        )
        if not match:
            continue
        prompt, cached, completion = match.groups()[1:]
        prices[model_id] = {
            "prompt_micro_per_m": _money_to_micro(prompt),
            "prompt_cached_micro_per_m": _money_to_micro(cached),
            "completion_micro_per_m": _money_to_micro(completion),
        }
    return prices
