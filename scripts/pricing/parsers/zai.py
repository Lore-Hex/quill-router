# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses docs.z.ai/guides/overview/pricing as rendered by r.jina.ai.
# Captured fixture lives at tests/fixtures/pricing/zai.html.
#
# Page format: Jina returns a clean markdown table.
#
#   | Model | Input | Cached Input | Cached Input Storage | Output |
#   | --- | --- | --- | --- | --- |
#   | GLM-5.1 | $1.4 | $0.26 | Limited-time Free | $4.4 |
#   | GLM-4.6 | $0.6 | $0.11 | Limited-time Free | $2.2 |
#   ...
#
# Z.AI's GLM family is single-tier (no context conditioning), so we
# emit flat ModelPrice rows.
"""Z.AI / Zhipu pricing parser (Jina-rendered markdown)."""
from __future__ import annotations

import re

# Display name on docs.z.ai → OR-canonical id.
_NAME_TO_OR_ID = {
    "GLM-5.1": "z-ai/glm-5.1",
    "GLM-5": "z-ai/glm-5",
    "GLM-5-Turbo": "z-ai/glm-5-turbo",
    "GLM-4.7": "z-ai/glm-4.7",
    "GLM-4.7-FlashX": "z-ai/glm-4.7-flashx",
    "GLM-4.6": "z-ai/glm-4.6",
    "GLM-4.5": "z-ai/glm-4.5",
    "GLM-4.5-X": "z-ai/glm-4.5-x",
    "GLM-4.5-Air": "z-ai/glm-4.5-air",
    "GLM-4.5-AirX": "z-ai/glm-4.5-airx",
    "GLM-4-32B-0414-128K": "z-ai/glm-4-32b",
    # Free tier models — keep them for catalog completeness; the floor
    # in catalog.py prevents them from advertising as $0/M.
    "GLM-4.7-Flash": "z-ai/glm-4.7-flash",
    "GLM-4.5-Flash": "z-ai/glm-4.5-flash",
    # Vision models share OR canonical ids.
    "GLM-5V-Turbo": "z-ai/glm-5v-turbo",
    "GLM-4.6V": "z-ai/glm-4.6v",
    "GLM-4.5V": "z-ai/glm-4.5v",
}


_DOLLAR_RE = re.compile(r"\$([\d.]+)")


def _to_micro_per_m(text: str | None) -> int:
    """Parse a price cell. Returns 0 for free / "-" / missing."""
    if not text:
        return 0
    if "free" in text.lower() or text.strip() in {"-", "\\"}:
        return 0
    match = _DOLLAR_RE.search(text)
    if not match:
        return 0
    try:
        return int(round(float(match.group(1)) * 1_000_000))
    except (TypeError, ValueError):
        return 0


def parse(md: str) -> dict:
    out: dict = {}
    for line in md.splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if not cells:
            continue
        name = cells[0]
        or_id = _NAME_TO_OR_ID.get(name)
        if or_id is None:
            continue
        if or_id in out:
            continue
        # Text models table: Model | Input | Cached Input | Cached
        # Input Storage | Output  → 5 cells. Cached Input cell is "-"
        # for some models; _to_micro_per_m returns 0 for that, which
        # we treat as "no cached rate" rather than "$0/M cached".
        if len(cells) >= 5:
            prompt = _to_micro_per_m(cells[1])
            cached = _to_micro_per_m(cells[2])
            completion = _to_micro_per_m(cells[4])
            row_out: dict = {
                "prompt_micro_per_m": prompt,
                "completion_micro_per_m": completion,
            }
            # Z.AI's "Limited-time Free" cells are caught as 0 by
            # _to_micro_per_m. Only emit prompt_cached when it's a
            # real positive number.
            if cached > 0:
                row_out["prompt_cached_micro_per_m"] = cached
            out[or_id] = row_out
    return out
