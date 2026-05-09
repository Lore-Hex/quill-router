# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses docs.venice.ai/overview/pricing via Jina. The page emits a
# repeating block per model:
#
#   GLM 4.6`zai-org-glm-4.6`
#
#   Input Price$0.85 Output Price$2.75 Cache Read$0.30 Context 198K
#
# Other rows have no Cache Read field. We pull the backtick-quoted
# native id and the Input/Output dollar amounts.
"""Venice pricing parser (Jina-rendered markdown)."""
from __future__ import annotations

import re

# Native Venice id (in backticks on the page) → OR-canonical id.
# Venice uses dash-separated forms ("zai-org-glm-4.6") that don't quite
# match OR's canonical ("z-ai/glm-4.6"). Map the families we route to.
_NAME_TO_OR_ID = {
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


_BLOCK_RE = re.compile(
    r"`([\w.\-]+)`"                                          # native id in backticks
    r"[\s\S]{0,300}?"                                         # short window (next price block)
    r"Input Price\$([\d.]+)"
    r"\s*Output Price\$([\d.]+)"
    r"(?:\s*Cache Read\$([\d.]+))?",                         # optional cached rate
)


def parse(md: str) -> dict:
    out: dict = {}
    for match in _BLOCK_RE.finditer(md):
        native, input_usd, output_usd, cached_usd = match.groups()
        or_id = _NAME_TO_OR_ID.get(native)
        if or_id is None:
            continue
        if or_id in out:
            continue
        try:
            input_micro = int(round(float(input_usd) * 1_000_000))
            output_micro = int(round(float(output_usd) * 1_000_000))
        except ValueError:
            continue
        row_out: dict = {
            "prompt_micro_per_m": input_micro,
            "completion_micro_per_m": output_micro,
        }
        if cached_usd:
            try:
                row_out["prompt_cached_micro_per_m"] = int(
                    round(float(cached_usd) * 1_000_000)
                )
            except ValueError:
                pass
        out[or_id] = row_out
    return out
