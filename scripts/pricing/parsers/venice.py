"""Venice pricing parser (Jina-rendered markdown table)."""
from __future__ import annotations

import re

# Native Venice id (in backticks on the page) → OR-canonical id.
_NAME_TO_OR_ID = {
    "zai-org-glm-5-2": "z-ai/glm-5.2",
    "zai-org-glm-5-1": "z-ai/glm-5.1",
    "zai-org-glm-5": "z-ai/glm-5",
    "z-ai-glm-5-turbo": "z-ai/glm-5-turbo",
    "z-ai-glm-5v-turbo": "z-ai/glm-5v-turbo",
    "zai-org-glm-4.7-flash": "z-ai/glm-4.7-flash",
    "zai-org-glm-4.7": "z-ai/glm-4.7",
    "zai-org-glm-4.6": "z-ai/glm-4.6",
    "qwen3-6-27b": "qwen/qwen3.6-27b",
    "qwen3-5-9b": "qwen/qwen3.5-9b",
    "qwen3-5-397b-a17b": "qwen/qwen3.5-397b-a17b",
    "qwen3-235b-a22b-thinking-2507": "qwen/qwen3-235b-a22b-thinking-2507",
    "qwen3-235b-a22b-instruct-2507": "qwen/qwen3-235b-a22b-instruct-2507",
    "qwen3-next-80b": "qwen/qwen3-next-80b",
    "qwen3-vl-235b-a22b": "qwen/qwen3-vl-235b-a22b",
    "qwen3-coder-480b-a35b-instruct-turbo": "qwen/qwen3-coder-480b-a35b-instruct-turbo",
}


# Match a chat-completions table row:
#   | Display | `native-id` | $X.XX | $Y.YY | $Z.ZZ | ... |
# Cache-read column may be "-" or "$N.NN".
_ROW_RE = re.compile(
    r"\|[^|\n]*\|\s*`([\w.\-]+)`\s*\|"      # native id
    r"\s*\$([\d.]+)\s*\|"                     # input
    r"\s*\$([\d.]+)\s*\|"                     # output
    r"\s*(?:\$([\d.]+)|-)\s*\|"               # optional cache read (or "-")
)


def _to_micro(value_usd: str) -> int:
    return int(round(float(value_usd) * 1_000_000))


def parse(md: str) -> dict:
    out: dict = {}
    for match in _ROW_RE.finditer(md):
        native, input_usd, output_usd, cached_usd = match.groups()
        or_id = _NAME_TO_OR_ID.get(native)
        if or_id is None:
            continue
        if or_id in out:
            continue
        try:
            row_out: dict = {
                "prompt_micro_per_m": _to_micro(input_usd),
                "completion_micro_per_m": _to_micro(output_usd),
            }
        except ValueError:
            continue
        if cached_usd:
            try:
                row_out["prompt_cached_micro_per_m"] = _to_micro(cached_usd)
            except ValueError:
                pass
        out[or_id] = row_out
    return out