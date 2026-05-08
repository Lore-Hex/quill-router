# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Gemini pricing has multiple surfaces, and this parser intentionally
# only covers what TR routes most-often:
#
#   * ai.google.dev (AI Studio) — what TR actually pays. We hold a
#     `GEMINI_API_KEY`, which routes to this surface. Pricing is
#     tier-conditional:
#       - Gemini 2.5 Pro: $1.25/M in, $10/M out for prompts ≤200k
#         context — DOUBLES to $2.50 / $15 for prompts >200k.
#       - Gemini 2.5 Flash: $0.30/M in, $2.50/M out (thinking mode).
#         Non-thinking mode is much cheaper but addressed by a
#         different model id, not a request flag, in OR's view.
#   * cloud.google.com/vertex-ai — Vertex SKUs, different IAM scope,
#     similar headline rates but with extra GCP serving costs. TR
#     does not route to Vertex (no GCP project quota), so these are
#     ignored.
#   * Free tier — non-commercial only; never applicable to paid TR.
#
# The hardcoded values below match AI Studio's ≤200k-context tier for
# the 3 Gemini models that appear in TR's auto-router. Everything else
# (Gemini 3, 3.1, image models, previews, all 2.0 variants) flows
# through OR's catalog via the merge step in refresh.py — provider-
# direct returns nothing for those rows, so the OR price wins.
#
# When Google ships a new headline Gemini model that lands in
# DEFAULT_AUTO_MODEL_ORDER, add it here. Otherwise let OR cover.
"""Google Gemini parser — small hardcoded subset, OR covers the rest."""
from __future__ import annotations


# OR-canonical id → (prompt $/M, completion $/M). Only the models TR
# routes by default. Verified against OR snapshot 2026-05-08.
_HARDCODED_PRICES: dict[str, tuple[float, float]] = {
    "google/gemini-2.5-pro": (1.25, 10.00),
    "google/gemini-2.5-flash": (0.30, 2.50),
    "google/gemini-2.5-flash-lite": (0.10, 0.40),
}


def parse(_html: str) -> dict:
    out: dict = {}
    for or_id, (prompt_usd, completion_usd) in _HARDCODED_PRICES.items():
        out[or_id] = {
            "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
            "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
        }
    return out
