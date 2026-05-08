# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Gemini pricing has multiple surfaces, and this parser intentionally
# only covers what TR routes most-often:
#
#   * ai.google.dev (AI Studio) — what TR actually pays. We hold a
#     `GEMINI_API_KEY`, which routes to this surface.
#   * Vertex (cloud.google.com) is ignored — TR has no GCP project quota.
#   * Free tier — non-commercial only; never applicable to paid TR.
#
# Pricing is CONTEXT-TIERED on Gemini 2.5 Pro: prompts ≤200k tokens
# pay $1.25/M input + $10/M output; prompts >200k pay $2.50/M + $15/M.
# This parser emits a tiered price profile for that case so the
# billing path can charge the right rate per request. Other Gemini
# models are flat single-tier.
#
# Captured manually from ai.google.dev/pricing on 2026-05-08.
"""Google Gemini parser — small hardcoded subset with tier support."""
from __future__ import annotations


def _flat(prompt_usd: float, completion_usd: float) -> dict:
    return {
        "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
        "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
    }


def _tier(
    max_prompt_tokens: int | None,
    prompt_usd: float,
    completion_usd: float,
) -> dict:
    return {
        "max_prompt_tokens": max_prompt_tokens,
        "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
        "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
    }


def parse(_html: str) -> dict:
    return {
        # Gemini 2.5 Pro: two-tier ≤200k vs >200k context.
        "google/gemini-2.5-pro": {
            "tiers": [
                _tier(200_000, 1.25, 10.00),
                _tier(None, 2.50, 15.00),
            ],
        },
        # Gemini 2.5 Flash: thinking-mode rate. Flat (no context tier).
        "google/gemini-2.5-flash": _flat(0.30, 2.50),
        # Gemini 2.5 Flash Lite: flat.
        "google/gemini-2.5-flash-lite": _flat(0.10, 0.40),
    }
