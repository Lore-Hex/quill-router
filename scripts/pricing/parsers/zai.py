# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# docs.z.ai/guides/llm/glm-4.6 is JS-rendered; the captured HTML doesn't
# contain $/M-token price literals. This parser carries a hardcoded
# constants table; cross-check vs OpenRouter catches drift, and the LLM
# self-heal flow rewrites the table when prices change.
#
# Prices: manual capture from docs.z.ai/guides/llm/glm-4.6 on 2026-05-08.
"""Z.AI / Zhipu hardcoded-table parser (page is JS-rendered)."""
from __future__ import annotations

# OR-canonical id → (prompt $/M, completion $/M).
_HARDCODED_PRICES: dict[str, tuple[float, float]] = {
    "z-ai/glm-4.6": (0.60, 2.20),
    "z-ai/glm-4.5": (0.60, 2.20),
    "z-ai/glm-4.5-air": (0.20, 1.10),
    "z-ai/glm-4.5v": (0.60, 1.80),
}


def parse(_html: str) -> dict:
    out: dict = {}
    for or_id, (prompt_usd, completion_usd) in _HARDCODED_PRICES.items():
        out[or_id] = {
            "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
            "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
        }
    return out
