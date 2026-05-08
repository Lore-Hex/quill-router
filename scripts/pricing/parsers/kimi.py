# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses the concatenation of all Kimi pricing sub-pages
# (chat-k26.md / chat-k25.md / chat-k2.md / chat-v1.md). The
# providers/kimi.py orchestrator fetches each .md and joins them
# before calling parse(); we just need to handle the JSX-shaped
# table rows.
#
# Row format (5- and 4-cell variants):
#
#   ["kimi-k2.6", "1M tokens", <>{"$"}0.16</>, <>{"$"}0.95</>, <>{"$"}4.00</>, "262,144 tokens"]
#   ["moonshot-v1-8k", "1M tokens", <>{"$"}0.20</>, <>{"$"}2.00</>, "8,192 tokens"]
#
# K2 family has 5 cells before the context column: id, unit, cache
# hit input, cache MISS input (headline), output. V1 family has only
# 4: id, unit, input, output.
#
# We use the cache-MISS input price as the headline (matches what a
# fresh request pays). Cache hit is irrelevant for our billing —
# we don't track cache state per-request.
"""Kimi/Moonshot pricing parser (multi-subpage markdown)."""
from __future__ import annotations

import re

# Native model id → OR-canonical id.
_NAME_TO_OR_ID = {
    "kimi-k2.6": "moonshotai/kimi-k2.6",
    "kimi-k2.5": "moonshotai/kimi-k2.5",
    "kimi-k2-0905-preview": "moonshotai/kimi-k2-0905-preview",
    "kimi-k2-0711-preview": "moonshotai/kimi-k2-0711-preview",
    "kimi-k2-turbo-preview": "moonshotai/kimi-k2-turbo-preview",
    "kimi-k2-thinking": "moonshotai/kimi-k2-thinking",
    "kimi-k2-thinking-turbo": "moonshotai/kimi-k2-thinking-turbo",
    "moonshot-v1-8k": "moonshotai/moonshot-v1-8k",
    "moonshot-v1-32k": "moonshotai/moonshot-v1-32k",
    "moonshot-v1-128k": "moonshotai/moonshot-v1-128k",
}


# Match: ["model-id", "1M tokens", <>{"$"}X.X</>, <>{"$"}Y.Y</>, <>{"$"}Z.Z</>, "context"]
# OR  : ["model-id", "1M tokens", <>{"$"}X.X</>, <>{"$"}Y.Y</>, "context"]
_ROW_RE = re.compile(
    r'\["([^"]+)"\s*,\s*"1M tokens"\s*,'
    r'\s*<>\{"\$"\}([\d.]+)</>\s*,'      # column A (cache hit OR input)
    r'\s*<>\{"\$"\}([\d.]+)</>\s*,'      # column B (cache miss OR output)
    r'(?:\s*<>\{"\$"\}([\d.]+)</>\s*,)?' # column C (output, only K2 family)
    r'\s*"([^"]+)"\s*\]'                  # context window
)


def _to_micro_per_m(usd: str) -> int:
    return int(round(float(usd) * 1_000_000))


def parse(text: str) -> dict:
    out: dict = {}
    for match in _ROW_RE.finditer(text):
        native_id, col_a, col_b, col_c, _context = match.groups()
        or_id = _NAME_TO_OR_ID.get(native_id)
        if or_id is None:
            continue
        if or_id in out:
            continue
        if col_c is not None:
            # 5-column shape: cache_hit, cache_miss, output. Use cache_miss
            # as the headline input rate (what a fresh request pays).
            input_micro = _to_micro_per_m(col_b)
            output_micro = _to_micro_per_m(col_c)
        else:
            # 4-column shape (V1 family): just input, output.
            input_micro = _to_micro_per_m(col_a)
            output_micro = _to_micro_per_m(col_b)
        out[or_id] = {
            "prompt_micro_per_m": input_micro,
            "completion_micro_per_m": output_micro,
        }
    return out
