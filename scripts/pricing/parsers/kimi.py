# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# platform.moonshot.ai/pricing is fully JavaScript-rendered; the HTML
# we fetch is just the navigation chrome, no $/M-token literals visible.
# This parser carries a hardcoded constants table; cross-check vs
# OpenRouter catches drift, and the LLM self-heal flow can rewrite the
# table when prices change.
#
# Prices below: manual capture from platform.moonshot.ai/pricing on
# 2026-05-08.
"""Moonshot/Kimi hardcoded-table parser (page is JS-rendered)."""
from __future__ import annotations

# OR-canonical id → (prompt $/M, completion $/M).
_HARDCODED_PRICES: dict[str, tuple[float, float]] = {
    "moonshotai/kimi-k2.6": (0.60, 2.50),
    "moonshotai/kimi-k2.5": (0.60, 2.50),
    "moonshotai/kimi-k2": (0.30, 1.20),
}


def parse(_html: str) -> dict:
    out: dict = {}
    for or_id, (prompt_usd, completion_usd) in _HARDCODED_PRICES.items():
        out[or_id] = {
            "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
            "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
        }
    return out
