# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses novita.ai/pricing (Jina-rendered markdown). Each row looks like:
#
#   | [deepseek/deepseek-v4-flash](...) | 1,048,576 | $0.14 /Mt· Cache Read $0.028 /Mt | $0.28 /Mt | [More](...) |
#
# Columns: model_id (in link text) | context | input | output | (more link).
# We extract the bracketed model id, input $-amount, output $-amount.
"""Novita pricing parser (Jina-rendered markdown)."""
from __future__ import annotations

import re

# Native model id (as shown in the link text) → OR-canonical id.
# Novita already uses OR-canonical-style ids ("deepseek/deepseek-v3.2"),
# so most ids pass through unchanged.
_NAME_TO_OR_ID: dict[str, str] = {}  # populated by _normalize


def _normalize(native_id: str) -> str:
    """Novita's ids match OR's convention closely (`vendor/model-name`),
    so we pass them through unchanged unless an explicit override is
    specified in _NAME_TO_OR_ID."""
    return _NAME_TO_OR_ID.get(native_id, native_id)


_LINK_PRICE_RE = re.compile(
    r"\| \[([\w./:_\-]+)\][^|]*"           # model id link
    r"\|\s*[\d,]+\s*"                       # context
    r"\|\s*\$([\d.]+)[^|]*"                 # input price (optionally with cache-read)
    r"\|\s*\$([\d.]+)[^|]*"                 # output price
    r"\|"
)


def parse(md: str) -> dict:
    out: dict = {}
    for match in _LINK_PRICE_RE.finditer(md):
        native, input_usd, output_usd = match.groups()
        or_id = _normalize(native)
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
