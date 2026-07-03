"""Parse Makora's public homepage model lineup pricing."""

from __future__ import annotations

import re
from decimal import Decimal

MODEL_LABELS: dict[str, tuple[str, ...]] = {
    "GLM-5.2": ("z-ai/glm-5.2", "z-ai/glm-5.2-nvfp4"),
    "Kimi-K2.7-Code": ("moonshotai/kimi-k2.7-code",),
    "DeepSeek-V4-Pro": ("deepseek/deepseek-v4-pro",),
    "Qwen3.6-27B-NVFP4": ("qwen/qwen3.6-27b",),
    "Llama-3.3-70B-Instruct": (
        "meta-llama/llama-3.3-70b-instruct",
        "amd/llama-3.3-70b-instruct-fp8-kv",
    ),
    "DeepSeek V4 Flash": ("deepseek/deepseek-v4-flash",),
    "Qwen3.6-35B-A3B": ("qwen/qwen3.6-35b-a3b",),
}


def _money_to_micro(raw: str) -> int:
    return int((Decimal(raw) * Decimal(1_000_000)).to_integral_value())


def _section_for_label(text: str, label: str) -> str:
    match = re.search(rf"(?<![A-Za-z0-9._-]){re.escape(label)}(?![A-Za-z0-9._-])", text)
    if not match:
        return ""
    tail = text[match.start() :]
    # Makora's Jina-rendered markdown repeats "[Try Now]" at the end
    # of each model card; bounding by it avoids accidentally reading
    # prices from the following card if a field disappears.
    end = tail.find("[Try Now]")
    if end != -1:
        return tail[:end]
    return tail[:800]


def _field(section: str, label: str) -> str | None:
    match = re.search(
        rf"{label}\s+\$([0-9]+(?:\.[0-9]+)?)\s*/\s*M\s+tokens",
        section,
        flags=re.I,
    )
    return match.group(1) if match else None


def parse(html: str) -> dict:
    text = re.sub(r"\s+", " ", html)
    out: dict[str, dict[str, int]] = {}
    for label, model_ids in MODEL_LABELS.items():
        section = _section_for_label(text, label)
        if not section:
            continue
        prompt = _field(section, "Input")
        completion = _field(section, "Output")
        cache = _field(section, r"Cache(?:\s+Read)?")
        if prompt is None or completion is None:
            continue
        row = {
            "prompt_micro_per_m": _money_to_micro(prompt),
            "completion_micro_per_m": _money_to_micro(completion),
        }
        if cache is not None:
            row["prompt_cached_micro_per_m"] = _money_to_micro(cache)
        for model_id in model_ids:
            out[model_id] = dict(row)
    return out
