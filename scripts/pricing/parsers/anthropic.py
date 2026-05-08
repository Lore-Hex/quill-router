# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
# This file MUST contain exactly one top-level function `parse(html: str) -> dict`.
# No imports outside whitelist (re, bs4, decimal, json, typing, dataclasses).
# No network IO, no filesystem, no subprocess, no eval/exec.
"""Anthropic pricing-page parser.

Initial heuristic parser. If Anthropic redesigns the page and the
patterns below stop matching, the hourly refresh's self-heal flow
will overwrite this file with an LLM-rewritten version.
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

# Mapping: page-displayed model name → OR-canonical model id.
# Extended over time as Anthropic adds models.
_NAME_TO_OR_ID = {
    "Claude Opus 4.7": "anthropic/claude-opus-4.7",
    "Claude Opus 4": "anthropic/claude-opus-4.7",
    "Claude Sonnet 4.6": "anthropic/claude-sonnet-4.6",
    "Claude Sonnet 4": "anthropic/claude-sonnet-4.6",
    "Claude Haiku 4.5": "anthropic/claude-haiku-4.5",
    "Claude Haiku 4": "anthropic/claude-haiku-4.5",
}

_DOLLAR_PER_M_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*/\s*(?:M|million)\b", re.IGNORECASE)


def _parse_dollar_to_micro_per_m(text: str) -> int | None:
    match = _DOLLAR_PER_M_RE.search(text)
    if match is None:
        return None
    raw = match.group(1).replace(",", "")
    try:
        usd_per_m = float(raw)
    except ValueError:
        return None
    return int(round(usd_per_m * 1_000_000))


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}

    # Anthropic's pricing page typically uses a card or table structure
    # with "$X / Mtok" and "$Y / Mtok" labels for input vs output.
    for card in soup.find_all(["div", "section", "tr", "li"]):
        text = card.get_text(" ", strip=True)
        if not text:
            continue
        for display_name, or_id in _NAME_TO_OR_ID.items():
            if display_name not in text:
                continue
            # Look for two distinct dollar/M values in this card.
            matches = _DOLLAR_PER_M_RE.findall(text)
            if len(matches) < 2:
                continue
            try:
                prompt_usd = float(matches[0].replace(",", ""))
                completion_usd = float(matches[1].replace(",", ""))
            except ValueError:
                continue
            out[or_id] = {
                "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
                "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
            }
            break

    return out
