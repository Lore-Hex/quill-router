# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses docs.x.ai/developers/pricing (Jina-rendered markdown). The page
# has a single chat-models table:
#
#   | [grok-4.3](https://docs.x.ai/developers/models/grok-4.3) | 1M | $1.25 | $0.20 | $2.50 |
#   | [grok-4.20-multi-agent-0309](...) | 2M | $1.25 | $0.20 | $2.50 |
#
# Columns are: Model | Context | Input | Cached | Output. The model
# cell is a markdown link — strip the [text](url) wrapper to extract
# the slug. Image / video / TTS rows have only 3 cells and are skipped
# because they don't have the Input + Output columns we need for cost.
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
# Markdown link wrapper [name](url) — extract just the name. Also tolerate
# `**name**` or bare names so the parser keeps working if the page sheds
# its link decoration in a future redesign.
_MD_LINK_RE = re.compile(r"^\s*\**\s*\[([^\]]+)\]\([^)]+\)\s*\**\s*$")


def _strip_link(cell: str) -> str:
    match = _MD_LINK_RE.match(cell)
    return match.group(1).strip() if match else cell.strip().strip("*").strip()


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
        # New layout has 5 cells: Model | Context | Input | Cached | Output.
        # Pricing-only rows (image/video) have 2 cells and skip the check
        # below. Old layout was 5 cells with cached last, so we detect the
        # layout heuristically: if cells[3] looks like the output price
        # (always > cached when both present) we treat it as old layout.
        if len(cells) < 5:
            continue
        name = _strip_link(cells[0])
        or_id = _NAME_TO_OR_ID.get(name)
        if or_id is None:
            continue
        if or_id in out:
            continue
        prompt = _to_micro(cells[2])
        # New page: cells[3] is cached input, cells[4] is output.
        # Old page: cells[3] was output, cells[4] was cached input.
        # Pick the LARGER of cells[3] and cells[4] as completion — output
        # is always strictly greater than the cached-input rate for the
        # chat models in this table (cached rate is by definition a
        # discount). Falls through correctly whichever column ordering
        # the page is currently in.
        cell3 = _to_micro(cells[3])
        cell4 = _to_micro(cells[4]) if len(cells) >= 5 else None
        candidates = [c for c in (cell3, cell4) if c is not None]
        if prompt is None or not candidates:
            continue
        completion = max(candidates)
        out[or_id] = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
    return out
