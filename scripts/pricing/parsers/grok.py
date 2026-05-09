# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses docs.x.ai/docs/models (Jina-rendered markdown). The page has
# a single chat-models table:
#
#   | grok-4.3 | 1M | $1.25 | $2.50 |  |
#   | grok-4.20-multi-agent-0309 | 2M | $1.25 | $2.50 |  |
#
# Columns are: model_id | context | input | output | (cached/empty).
# Image / video / TTS rows have a different shape and are skipped.
"""xAI Grok pricing parser (Jina-rendered markdown)."""
from __future__ import annotations

import re

_NAME_TO_OR_ID = {
    "grok-4.3": "x-ai/grok-4.3",
    "grok-4.20-multi-agent-0309": "x-ai/grok-4.20-multi-agent",
    "grok-4.20-0309-reasoning": "x-ai/grok-4.20-reasoning",
    "grok-4.20-0309-non-reasoning": "x-ai/grok-4.20",
    "grok-4-1-fast-reasoning": "x-ai/grok-4-1-fast-reasoning",
    "grok-4-1-fast-non-reasoning": "x-ai/grok-4-1-fast",
}

_DOLLAR_RE = re.compile(r"\$([\d.]+)")


def _to_micro(text: str) -> int | None:
    if not text or text.strip() == "-":
        return None
    match = _DOLLAR_RE.search(text)
    if not match:
        return None
    try:
        return int(round(float(match.group(1)) * 1_000_000))
    except (TypeError, ValueError):
        return None


def parse(md: str) -> dict:
    out: dict = {}
    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        name = cells[0]
        or_id = _NAME_TO_OR_ID.get(name)
        if or_id is None:
            continue
        if or_id in out:
            continue
        # Skip rows that don't have $-amounts in the input/output cells
        # (image/video/TTS rows have a different shape).
        prompt = _to_micro(cells[2])
        completion = _to_micro(cells[3])
        if prompt is None or completion is None:
            continue
        out[or_id] = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
    return out
