"""Parse Fireworks serverless pricing docs."""
from __future__ import annotations

import re
from decimal import Decimal

MODEL_LABELS = {
    "Kimi K2.7 Code": "moonshotai/kimi-k2.7-code",
    "Kimi K2.6": "moonshotai/kimi-k2.6",
    "Kimi K2.5": "moonshotai/kimi-k2.5",
    "DeepSeek V4 Pro": "deepseek/deepseek-v4-pro",
    "DeepSeek V4 Flash": "deepseek/deepseek-v4-flash",
    "GLM 5.1": "z-ai/glm-5.1",
    "OpenAI GPT OSS 120B": "openai/gpt-oss-120b",
    "OpenAI GPT OSS 20B": "openai/gpt-oss-20b",
    "MiniMax M3": "minimax/minimax-m3",
    "MiniMax 2.7": "minimax/minimax-m2.7",
    "MiniMax 2.5": "minimax/minimax-m2.5",
}


def _money_to_micro(raw: str) -> int:
    return int((Decimal(raw) * Decimal(1_000_000)).to_integral_value())


def parse(html: str) -> dict:
    text = re.sub(r"\s+", " ", html)
    out: dict[str, dict[str, int]] = {}
    for label, model_id in MODEL_LABELS.items():
        label_pattern = rf"(?:{re.escape(label)}|\[{re.escape(label)}\]\([^)]*\))"
        pattern = (
            label_pattern
            + r"(?:\s+Fast)?\s*\|?\s*\$([0-9]+(?:\.[0-9]+)?)\s*/\s*"
            + r"\$([0-9]+(?:\.[0-9]+)?)\s*/\s*"
            + r"\$([0-9]+(?:\.[0-9]+)?)"
        )
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        prompt, cached, completion = match.groups()
        out[model_id] = {
            "prompt_micro_per_m": _money_to_micro(prompt),
            "prompt_cached_micro_per_m": _money_to_micro(cached),
            "completion_micro_per_m": _money_to_micro(completion),
        }
    return out
