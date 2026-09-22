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


def _html_overseas_payg_prices(soup: BeautifulSoup) -> dict[str, dict[str, int]]:
    """Parse the rendered USD table without crossing into the RMB section."""
    heading = next(
        (
            tag
            for tag in soup.find_all(re.compile(r"^h[1-6]$"))
            if tag.get_text(" ", strip=True).casefold()
            == "overseas pricing of the model"
        ),
        None,
    )
    if heading is None:
        return {}

    next_section_node = heading.find_next(
        ["table", "h1", "h2", "h3", "h4", "h5", "h6"]
    )
    if next_section_node is None or next_section_node.name != "table":
        return {}
    table = next_section_node

    prices: dict[str, dict[str, int]] = {}
    typed_table = any(
        cell.get_text(" ", strip=True).casefold() == "inference type"
        for cell in table.find_all("th")
    )
    inference_type = "real-time api"
    remaining_rows = 0
    for row in table.find_all("tr"):
        cells = row.find_all("td")
        if not cells:
            continue
        if typed_table:
            if len(cells) == 5:
                inference_type = cells[0].get_text(" ", strip=True).casefold()
                remaining_rows = int(str(cells[0].get("rowspan", "1"))) - 1
                cells = cells[1:]
            elif len(cells) == 4 and remaining_rows > 0:
                remaining_rows -= 1
            else:
                raise ValueError("xiaomi: ambiguous inference-type row span")
            if inference_type != "real-time api":
                continue
        if len(cells) != 4:
            continue
        models = re.findall(
            r"\bmimo-[a-z0-9._-]+",
            cells[0].get_text(" ", strip=True),
            flags=re.I,
        )
        values = [
            re.fullmatch(r"\$\s*([0-9.]+)", cell.get_text(" ", strip=True))
            for cell in cells[1:4]
        ]
        if not models or any(value is None for value in values):
            continue
        cache, prompt, completion = (value.group(1) for value in values if value)
        price = {
            "prompt_micro_per_m": _money_to_micro_per_m(prompt),
            "completion_micro_per_m": _money_to_micro_per_m(completion),
            "prompt_cached_micro_per_m": _money_to_micro_per_m(cache),
        }
        for model in models:
            model_id = f"xiaomi/{model.casefold()}"
            if model_id in prices and prices[model_id] != price:
                raise ValueError("xiaomi: conflicting real-time USD prices")
            prices[model_id] = price
    return prices


def _markdown_overseas_payg_prices(html: str) -> dict[str, dict[str, int]]:
    """Parse the authoritative USD table from Markdown source or fixtures."""
    section_match = re.search(
        r"###\s*Overseas Pricing of the Model\s+(.*?)(?=###\s|$)",
        html,
        flags=re.I | re.S,
    )
    if not section_match:
        return {}
    # A flattened table loses row-span attribution. Never guess which rows
    # are discounted batch prices; the rendered HTML path preserves that data.
    if re.search(r"\bBatch\s+API\b", section_match.group(1), re.I):
        raise ValueError("xiaomi: batch pricing requires the rendered inference-type table")

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
    soup = BeautifulSoup(html, "html.parser")
    # Xiaomi now serves rendered HTML while older captures expose the source
    # Markdown. Parse the section boundary in either representation so the
    # preceding domestic RMB table can never be mistaken for USD pricing.
    prices = _html_overseas_payg_prices(soup) or _markdown_overseas_payg_prices(html)
    # Keep the card parser as a compatibility fallback for older captures and
    # for UltraSpeed if Xiaomi republishes its standalone PAYG card.
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
