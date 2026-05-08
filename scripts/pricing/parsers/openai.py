# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses platform.openai.com/docs/pricing as rendered by r.jina.ai.
# Captured fixture lives at tests/fixtures/pricing/openai.html.
#
# Page format: Jina returns clean markdown tables. The Standard tier
# tables look like:
#
#   |  | Short context | Long context |
#   | --- | --- | --- |
#   | Model | Input | Cached input | Output | Input | Cached input | Output |
#   | gpt-5.5 | $5.00 | $0.50 | $30.00 | $10.00 | $1.00 | $45.00 |
#   | gpt-5.4 | $2.50 | $0.25 | $15.00 | $5.00 | $0.50 | $22.50 |
#   ...
#
# Columns 1+3 are Short context (≤ some threshold) input/output;
# columns 4+6 are Long context input/output. We pull the Standard
# tier (the headline rate) and ignore Batch/Flex/Priority for now.
#
# OpenAI doesn't explicitly state the Short/Long threshold on the page,
# but their docs convention is 200k for the GPT-5 family. We emit a
# tiered ModelPrice when both columns are populated; otherwise flat.
"""OpenAI pricing parser (Jina-rendered markdown)."""
from __future__ import annotations

import re

# Display name on platform.openai.com/docs/pricing → OR-canonical id.
_NAME_TO_OR_ID = {
    "gpt-5.5": "openai/gpt-5.5",
    "gpt-5.5-pro": "openai/gpt-5.5-pro",
    "gpt-5.4": "openai/gpt-5.4",
    "gpt-5.4-mini": "openai/gpt-5.4-mini",
    "gpt-5.4-nano": "openai/gpt-5.4-nano",
    "gpt-5.4-pro": "openai/gpt-5.4-pro",
    "gpt-5.3-codex": "openai/gpt-5.3-codex",
}

# Threshold for OpenAI's Short context vs Long context tiers, in tokens.
# Not stated on the page but consistent with their GPT-5 family docs;
# 200k matches Gemini 2.5 Pro's tier shape.
_SHORT_CONTEXT_THRESHOLD = 200_000


_DOLLAR_RE = re.compile(r"\$([\d.]+)")


def _to_micro_per_m(text: str | None) -> int | None:
    if not text or text.strip() in {"-", ""}:
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
    # Walk every markdown table row that starts with "| " — Jina renders
    # OpenAI's HTML tables as pipe-delimited markdown.
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
            # First match wins — the Standard tier table appears first
            # on the page; later sections (Batch / Flex / Priority) have
            # discount rates we don't want to overwrite the headline with.
            continue
        # 6-column table (Standard / chat models): Input | Cached |
        # Output | Long Input | Long Cached | Long Output.
        if len(cells) >= 7:
            short_input = _to_micro_per_m(cells[1])
            short_output = _to_micro_per_m(cells[3])
            long_input = _to_micro_per_m(cells[4])
            long_output = _to_micro_per_m(cells[6])
            if short_input is None or short_output is None:
                continue
            if long_input is not None and long_output is not None:
                out[or_id] = {
                    "tiers": [
                        {
                            "max_prompt_tokens": _SHORT_CONTEXT_THRESHOLD,
                            "prompt_micro_per_m": short_input,
                            "completion_micro_per_m": short_output,
                        },
                        {
                            "max_prompt_tokens": None,
                            "prompt_micro_per_m": long_input,
                            "completion_micro_per_m": long_output,
                        },
                    ],
                }
            else:
                out[or_id] = {
                    "prompt_micro_per_m": short_input,
                    "completion_micro_per_m": short_output,
                }
            continue
        # 4-column table (single-context models like Codex /
        # transcription): Input | Cached | Output. Skip if shape
        # doesn't match what we expect.
        if len(cells) >= 4:
            single_input = _to_micro_per_m(cells[1])
            single_output = _to_micro_per_m(cells[3])
            if single_input is not None and single_output is not None:
                out[or_id] = {
                    "prompt_micro_per_m": single_input,
                    "completion_micro_per_m": single_output,
                }
    return out
