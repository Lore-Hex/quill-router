# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
# Initial version derived from a real fetch of www.cerebras.ai/pricing on
# 2026-05-08. Captured fixture lives at tests/fixtures/pricing/cerebras.html.
#
# Page structure: Cerebras runs a Sanity CMS that embeds the pricing table
# as a JSON blob inline. Each row is shaped:
#   "cells": ["[META]Llama 3.1 8B**", "~2200 tokens/s", "$$0.10/M tokens", "$$0.10/M tokens"]
# The model name carries a vendor-bracket prefix and trailing asterisks.
# We strip both and map to OR-canonical ids.
"""Cerebras pricing-page parser."""
from __future__ import annotations

import json
import re

# Display name on cerebras.ai (after stripping bracket+asterisk decoration)
# → OR-canonical id. Extended over time.
_NAME_TO_OR_ID = {
    "Llama 3.1 8B": "meta-llama/llama-3.1-8b-instruct",
    "Llama 3.1 70B": "meta-llama/llama-3.1-70b-instruct",
    "Llama 3.3 70B": "meta-llama/llama-3.3-70b-instruct",
    "Llama 4 Maverick": "meta-llama/llama-4-maverick",
    "Llama 4 Scout": "meta-llama/llama-4-scout",
    "GPT OSS 120B": "openai/gpt-oss-120b",
    "Qwen 3 235B Instruct": "qwen/qwen3-235b-a22b-2507",
    "ZAI GLM 4.7": "z-ai/glm-4.7",
}


_PRICE_RE = re.compile(r"\$+([\d.]+)\s*/\s*M\s*tokens", re.IGNORECASE)
_NAME_DECORATION_RE = re.compile(r"^\[[^\]]+\]\s*|\s*\*+\s*$")


def _strip_decoration(name: str) -> str:
    """Remove vendor-bracket prefix (`[META]`, `[OPENAI]`, ...) and any
    trailing asterisks Cerebras uses for footnote markers."""
    return _NAME_DECORATION_RE.sub("", name).strip()


def _price_to_micro_per_m(text: str) -> int | None:
    match = _PRICE_RE.search(text or "")
    if match is None:
        return None
    try:
        return int(round(float(match.group(1)) * 1_000_000))
    except (TypeError, ValueError):
        return None


def parse(html: str) -> dict:
    out: dict = {}
    # Cerebras' Sanity CMS payload is embedded as an escaped JSON string
    # in the HTML — `\"cells\":[\"[META]Llama 3.1 8B**\", ...]`. The
    # cell values themselves contain `]` characters (vendor brackets like
    # `[META]`), so a simple `[^\]]+` regex stops too early. We
    # un-escape the whole HTML first and then match `cells` arrays of
    # exactly 4 quoted strings — the schema Cerebras uses.
    text = html.replace('\\"', '"')
    # Match a `cells` array of exactly 4 quoted strings — Cerebras's
    # row schema. The captured group is the full bracketed array,
    # which we re-parse via `json.loads`.
    for match in re.finditer(
        r'"cells"\s*:\s*(\[\s*"[^"]*"(?:\s*,\s*"[^"]*"){3}\s*\])',
        text,
    ):
        cells_text = match.group(1)
        try:
            cells = json.loads(cells_text)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(cells, list) or len(cells) != 4:
            continue
        raw_name, _speed, raw_input, raw_output = cells
        if not isinstance(raw_name, str):
            continue
        name = _strip_decoration(raw_name)
        or_id = _NAME_TO_OR_ID.get(name)
        if or_id is None:
            continue
        if or_id in out:
            continue
        prompt = _price_to_micro_per_m(
            raw_input if isinstance(raw_input, str) else ""
        )
        completion = _price_to_micro_per_m(
            raw_output if isinstance(raw_output, str) else ""
        )
        if prompt is None or completion is None:
            continue
        out[or_id] = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
    return out
