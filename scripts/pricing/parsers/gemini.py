# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Both ai.google.dev/pricing (OAuth-redirects) and the Vertex
# cloud.google.com/vertex-ai/generative-ai/pricing page have problems:
#   * ai.google.dev — 302 to OAuth, never serves HTML
#   * cloud.google.com — server-renders Anthropic-on-Vertex / Grok /
#     DeepSeek prices, but Gemini 2.5 Pro / Flash / Flash-Lite headline
#     rates are populated via JavaScript and not in the captured HTML.
#
# Hardcoded constants table for the Gemini models we route. Cross-check
# vs OpenRouter catches drift; LLM self-heal rewrites this table when
# prices change. Captured manually from ai.google.dev/pricing on
# 2026-05-08 via a logged-in session.
"""Google Gemini hardcoded-table parser (Vertex page is partly JS-rendered)."""
from __future__ import annotations

# OR-canonical id → (prompt $/M, completion $/M).
_HARDCODED_PRICES: dict[str, tuple[float, float]] = {
    "google/gemini-2.5-pro": (1.25, 10.00),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
    "google/gemini-2.0-flash-001": (0.10, 0.40),
    "google/gemini-2.0-flash-lite-001": (0.075, 0.30),
    "google/gemini-1.5-flash": (0.075, 0.30),
    "google/gemini-1.5-pro": (1.25, 5.00),
}


def parse(_html: str) -> dict:
    out: dict = {}
    for or_id, (prompt_usd, completion_usd) in _HARDCODED_PRICES.items():
        out[or_id] = {
            "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
            "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
        }
    return out
