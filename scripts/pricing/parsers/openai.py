# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
"""OpenAI pricing-page parser (initial heuristic)."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

_NAME_TO_OR_ID = {
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "GPT-4o mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o",
    "GPT-4o": "openai/gpt-4o",
}

_DOLLAR_PER_M_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*/\s*(?:M|million)\b", re.IGNORECASE)


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}

    for tr in soup.find_all(["tr", "div", "section", "li"]):
        text = tr.get_text(" ", strip=True)
        if not text:
            continue
        for display_name, or_id in _NAME_TO_OR_ID.items():
            if display_name not in text:
                continue
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
