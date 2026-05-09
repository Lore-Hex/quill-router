# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses red-pill.ai/ (Phala's marketing landing page) via Jina.
# The page renders each model as a card with inline price text:
#
#   "Qwen: Qwen3.5-27B GPU TEE ... by phala|262K context|$0.30/M input|$2.40/M output Intel TDX NVIDIA CC"
#
# Each card has a `[Display: model-id](https://red-pill.ai/models/<vendor>/<slug>)`
# link, plus the pipe-delimited price stanza. We pull the link href
# (which carries the canonical id) and the two $-amounts.
"""Phala (RedPill) pricing parser (Jina-rendered markdown)."""
from __future__ import annotations

import re

# Each card is a markdown link `[label]( url )`. The label has the
# pricing inline ("|$0.30/M input|$2.40/M output"); the URL comes
# AFTER the closing `]`. So we anchor on the price, then look ahead
# for the URL within the same link.
_CARD_RE = re.compile(
    r"\$([\d.]+)\s*/M\s*input"
    r"[^[\]]{0,200}?"
    r"\$([\d.]+)\s*/M\s*output"
    r"[^[\]]{0,500}?"
    r"\]\(https://red-pill\.ai/models/([\w.\-/_]+)\)",
    re.DOTALL,
)


def parse(md: str) -> dict:
    out: dict = {}
    for match in _CARD_RE.finditer(md):
        input_usd, output_usd, slug = match.groups()
        # Phala IDs are vendor/model. Map to TR-canonical: phala/<...>
        # for first-party ("phala/") models, vendor/<...> otherwise
        # (e.g., qwen/qwen3.5-27b stays qwen/qwen3.5-27b — same as OR).
        or_id = slug
        if or_id in out:
            continue
        try:
            input_micro = int(round(float(input_usd) * 1_000_000))
            output_micro = int(round(float(output_usd) * 1_000_000))
        except ValueError:
            continue
        out[or_id] = {
            "prompt_micro_per_m": input_micro,
            "completion_micro_per_m": output_micro,
        }
    return out
