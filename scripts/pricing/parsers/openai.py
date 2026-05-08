# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# OpenAI's pricing page (openai.com/api/pricing/) is Cloudflare-blocked
# from script User-Agents — every fetch returns 403 even with a real
# browser UA. The captured fixture (tests/fixtures/pricing/openai.html)
# is the 9.8KB block page itself, not real pricing.
#
# So this parser carries a hardcoded constants table for the OpenAI
# models we route. The cross-check vs OpenRouter catches drift; when
# OpenAI raises prices, the OR cross-check disagreement is logged in
# the commit body and the LLM self-heal kicks in to update the table.
# Prices below are taken from openai.com/api/pricing as of 2026-05-08
# (manual capture — the only fixture we have for this provider).
"""OpenAI hardcoded-table parser (page is Cloudflare-blocked)."""
from __future__ import annotations

# OR-canonical id → (prompt $/M, completion $/M). Manually maintained.
# Source: openai.com/api/pricing (manual capture 2026-05-08).
_HARDCODED_PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.50, 10.00),
    "openai/gpt-4.1": (2.00, 8.00),
    "openai/gpt-4.1-mini": (0.40, 1.60),
    "openai/gpt-4.1-nano": (0.10, 0.40),
    "openai/o1-mini": (1.10, 4.40),
    "openai/o1": (15.00, 60.00),
    "openai/o3-mini": (1.10, 4.40),
    "openai/o3": (10.00, 40.00),
}


def parse(_html: str) -> dict:
    """Returns the hardcoded price table regardless of input HTML.
    The argument is ignored — see module docstring.
    """
    out: dict = {}
    for or_id, (prompt_usd, completion_usd) in _HARDCODED_PRICES.items():
        out[or_id] = {
            "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
            "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
        }
    return out
