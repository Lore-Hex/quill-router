"""Parser for Xiaomi MiMo API pricing."""

from __future__ import annotations

import re
from decimal import Decimal

from bs4 import BeautifulSoup


def _money_to_micro_per_m(value: str) -> int:
    return int((Decimal(value) * Decimal(1_000_000)).to_integral_value())


def _section(text: str, title: str) -> str:
    pattern = rf"####\s*{re.escape(title)}\s+(.*?)(?=####\s*MiMo-|##\s|$)"
    match = re.search(pattern, text, re.S)
    return match.group(1) if match else ""


def _overseas_payg_prices(html: str) -> dict[str, dict[str, int]]:
    """Parse the authoritative USD table from Xiaomi's PAYG page."""
    section_match = re.search(
        r"###\s*Overseas Pricing of the Model\s+(.*?)(?=###\s|$)",
        html,
        flags=re.I | re.S,
    )
    if not section_match:
        return {}

    prices: dict[str, dict[str, int]] = {}
    table_row_pattern = re.compile(
        r"\|\s*`?(mimo-[a-z0-9._-]+)`?\s*"
        r"\|\s*\$([0-9.]+)\s*"
        r"\|\s*\$([0-9.]+)\s*"
        r"\|\s*\$([0-9.]+)\s*\|",
        flags=re.I,
    )
    # Some renderers flatten the Markdown table while preserving the model and
    # currency markers. Keep the three-dollar requirement so the preceding RMB
    # table can never be interpreted as USD.
    flat_row_pattern = re.compile(
        r"`?(mimo-[a-z0-9._-]+)`?\s+"
        r"\$([0-9.]+)\s+\$([0-9.]+)\s+\$([0-9.]+)",
        flags=re.I,
    )
    section = section_match.group(1)
    rows = table_row_pattern.findall(section)
    if not rows:
        rows = flat_row_pattern.findall(section)
    for model, cache, prompt, completion in rows:
        model_id = f"xiaomi/{model.casefold()}"
        prices[model_id] = {
            "prompt_micro_per_m": _money_to_micro_per_m(prompt),
            "completion_micro_per_m": _money_to_micro_per_m(completion),
            "prompt_cached_micro_per_m": _money_to_micro_per_m(cache),
        }
    return prices


def parse(html: str) -> dict[str, dict[str, int]]:
    # The current official page publishes a Markdown table. Keep the
    # card parser below as a compatibility fallback for older captures and
    # for UltraSpeed if Xiaomi republishes its standalone PAYG card.
    prices = _overseas_payg_prices(html)
    soup = BeautifulSoup(html, "html.parser")
    for heading in soup.find_all("h4"):
        title = heading.get_text(" ", strip=True)
        if not re.fullmatch(r"MiMo-[A-Za-z0-9._-]+", title, flags=re.I):
            continue
        container = heading.parent
        while container is not None:
            if len(container.find_all("h4")) > 1:
                container = None
                break
            block = container.get_text(" ", strip=True)
            if re.search(r"Input\s*\(cache\s+miss\)", block, flags=re.I) and re.search(
                r"\bOutput\b",
                block,
                flags=re.I,
            ):
                break
            container = container.parent
        if container is None:
            continue
        block = container.get_text(" ", strip=True)
        cache = re.search(r"Input\s*\(cache\s+hit\)\s*\$\s*([0-9.]+)", block, flags=re.I)
        prompt = re.search(r"Input\s*\(cache\s+miss\)\s*\$\s*([0-9.]+)", block, flags=re.I)
        completion = re.search(r"\bOutput\s*\$\s*([0-9.]+)", block, flags=re.I)
        if not prompt or not completion:
            continue
        row = {
            "prompt_micro_per_m": _money_to_micro_per_m(prompt.group(1)),
            "completion_micro_per_m": _money_to_micro_per_m(completion.group(1)),
        }
        if cache:
            row["prompt_cached_micro_per_m"] = _money_to_micro_per_m(cache.group(1))
        prices.setdefault(f"xiaomi/{title.casefold()}", row)

    text = re.sub(r"\s+", " ", html)
    titles = dict.fromkeys(re.findall(r"####\s*(MiMo-[A-Za-z0-9._-]+)", html, flags=re.I))
    for title in titles:
        model_id = f"xiaomi/{title.casefold()}"
        block = _section(text, title)
        if not block:
            continue
        cache = re.search(r"Input \(cache hit\)\$([0-9.]+)\s*/\s*MTok", block)
        prompt = re.search(r"Input \(cache miss\)\$([0-9.]+)\s*/\s*MTok", block)
        completion = re.search(r"Output\$([0-9.]+)\s*/\s*MTok", block)
        if not prompt or not completion:
            continue
        row = {
            "prompt_micro_per_m": _money_to_micro_per_m(prompt.group(1)),
            "completion_micro_per_m": _money_to_micro_per_m(completion.group(1)),
        }
        if cache:
            row["prompt_cached_micro_per_m"] = _money_to_micro_per_m(cache.group(1))
        prices.setdefault(model_id, row)
    return prices
