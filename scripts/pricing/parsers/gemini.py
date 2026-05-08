# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
"""Google Gemini pricing-page parser (initial heuristic)."""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

_NAME_TO_OR_ID = {
    "Gemini 2.5 Flash": "google/gemini-2.5-flash",
    "gemini-2.5-flash": "google/gemini-2.5-flash",
    "Gemini 2.5 Pro": "google/gemini-2.5-pro",
    "gemini-2.5-pro": "google/gemini-2.5-pro",
}

_DOLLAR_PER_M_RE = re.compile(r"\$([\d,]+(?:\.\d+)?)\s*/\s*(?:1?M|million)\b", re.IGNORECASE)


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}

    for block in soup.find_all(["tr", "div", "section", "li", "table"]):
        text = block.get_text(" ", strip=True)
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
