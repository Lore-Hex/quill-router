# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
"""Anthropic pricing-page parser.

Handles multiple historical page layouts:
  * Webflow .card_pricing_title_text / .tokens_main_* cards (2024-2025).
  * CSS-module __modelName / __priceRow cards (Sep 2026).
  * Flat text / markdown-ish dumps (late 2026) where a model heading like
    "Haiku 5.5" is followed on subsequent lines by "Input", "$0.10", "/ MTok",
    "Output", "$0.50", "/ MTok", etc. Some models have tiered pricing with
    "Prompts ≤ 100K tokens" / "Prompts > 100K tokens" subsections; we take
    the first (standard) tier.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup

_MODEL_FAMILIES = {"Fable", "Opus", "Sonnet", "Haiku"}
_FAST_MODE_RE = re.compile(
    r"fast mode for (?P<family>Fable|Opus|Sonnet|Haiku) "
    r"(?P<version>[0-9]+(?:\.[0-9]+)?) at "
    r"(?P<multiplier>[0-9]+(?:\.[0-9]+)?)x standard pricing",
    re.IGNORECASE,
)
_MODEL_HEADING_RE = re.compile(
    r"^(?P<family>Fable|Opus|Sonnet|Haiku)\s+(?P<version>[0-9]+(?:\.[0-9]+)?)$"
)
_PRICE_RE = re.compile(r"^\$([0-9]+(?:\.[0-9]+)?)$")


def _is_model_name(name: str) -> tuple[str, str] | None:
    parts = name.split()
    if len(parts) == 2 and parts[0] in _MODEL_FAMILIES and parts[1].replace(".", "", 1).isdigit():
        return parts[0], parts[1]
    return None


def _parse_structured(soup: BeautifulSoup) -> dict:
    """Parse the Webflow / CSS-module layouts where each model lives in a card."""
    out: dict = {}
    headings = soup.select('h3.card_pricing_title_text, h3[class*="__modelName"]')
    for heading in headings:
        name = heading.get_text(strip=True)
        parsed = _is_model_name(name)
        if parsed is None:
            continue
        family, version = parsed
        or_id = f"anthropic/claude-{family.lower()}-{version}"
        card = heading.parent.parent if heading.parent else None
        if card is None:
            continue
        prices: dict[str, Decimal] = {}
        for price_node in card.select('.tokens_main_wrap, [class*="__priceRow"]'):
            label = price_node.select_one('.tokens_main_label, [class*="__priceLabel"]')
            value = price_node.select_one('.tokens_main_val_number, [class*="__priceValue"]')
            if label is None or value is None:
                continue
            lname = label.get_text(" ", strip=True).lower()
            if lname not in {"input", "output", "read", "write"} or lname in prices:
                continue
            try:
                raw_value = value.get("data-value")
                if not isinstance(raw_value, str):
                    raw_value = value.get_text(" ", strip=True)
                    m = re.fullmatch(r"\$([0-9]+(?:\.[0-9]+)?)\s*/\s*MTok", raw_value)
                    if m is not None:
                        raw_value = m.group(1)
                prices[lname] = Decimal(raw_value)
            except (InvalidOperation, TypeError, ValueError):
                continue
        if "input" not in prices or "output" not in prices:
            continue
        row = {
            "prompt_micro_per_m": int(prices["input"] * 1_000_000),
            "completion_micro_per_m": int(prices["output"] * 1_000_000),
        }
        if "read" in prices:
            row["prompt_cached_micro_per_m"] = int(prices["read"] * 1_000_000)
        out[or_id] = row
    return out


def _collect_lines(soup: BeautifulSoup) -> list[str]:
    """Return a flat list of non-empty text lines from the page."""
    lines: list[str] = []
    for s in soup.stripped_strings:
        text = s.strip()
        if text:
            lines.append(text)
    return lines


def _parse_flat_text(lines: list[str]) -> dict:
    """Parse the flat-text dump format.

    After a line matching e.g. "Haiku 5.5", scan forward until the next model
    heading, looking for label lines "Input"/"Output"/"Read"/"Write" each
    followed (within a few lines) by a "$X.YZ" line. For tiered models we
    take the first price that appears after each label.
    """
    out: dict = {}
    # Find indices of all model-heading lines.
    model_positions: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines):
        m = _MODEL_HEADING_RE.match(line)
        if m is not None:
            model_positions.append((i, m.group("family"), m.group("version")))

    for idx, (start, family, version) in enumerate(model_positions):
        end = model_positions[idx + 1][0] if idx + 1 < len(model_positions) else len(lines)
        segment = lines[start + 1 : end]
        or_id = f"anthropic/claude-{family.lower()}-{version}"
        if or_id in out:
            continue
        prices: dict[str, Decimal] = {}
        label_map = {
            "input": "input",
            "output": "output",
            "read": "read",
            "write": "write",
        }
        for j, line in enumerate(segment):
            key = label_map.get(line.lower())
            if key is None or key in prices:
                continue
            # Look ahead up to 6 lines for the first "$N" value.
            for k in range(j + 1, min(j + 7, len(segment))):
                pm = _PRICE_RE.match(segment[k])
                if pm is not None:
                    try:
                        prices[key] = Decimal(pm.group(1))
                    except InvalidOperation:
                        pass
                    break
                # If we hit another label before a price, stop looking.
                if segment[k].lower() in label_map:
                    break
        if "input" not in prices or "output" not in prices:
            continue
        row = {
            "prompt_micro_per_m": int(prices["input"] * 1_000_000),
            "completion_micro_per_m": int(prices["output"] * 1_000_000),
        }
        if "read" in prices:
            row["prompt_cached_micro_per_m"] = int(prices["read"] * 1_000_000)
        out[or_id] = row
    return out


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out = _parse_structured(soup)

    # Fallback / supplement: parse flat text for any models not already found.
    lines = _collect_lines(soup)
    flat = _parse_flat_text(lines)
    for key, value in flat.items():
        if key not in out:
            out[key] = value

    # Anthropic documents Fast mode as a multiplier below the standard model
    # cards. Publish a distinct `-fast` SKU only when the statement and base
    # card are both present.
    page_text = " ".join(lines) if lines else " ".join(soup.stripped_strings)
    for match in _FAST_MODE_RE.finditer(page_text):
        family = match.group("family").lower()
        version = match.group("version")
        base_id = f"anthropic/claude-{family}-{version}"
        base = out.get(base_id)
        if base is None:
            continue
        multiplier = Decimal(match.group("multiplier"))
        out[f"{base_id}-fast"] = {
            key: int(Decimal(value) * multiplier) for key, value in base.items()
        }
    return out
