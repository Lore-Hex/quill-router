# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
"""Anthropic pricing-page parser.

Handles multiple historical page layouts:
  * Webflow .card_pricing_title_text / .tokens_main_* cards (2024-2025).
  * CSS-module __modelName / __priceRow cards (Sep 2026).
  * Flat text / markdown-ish dumps (late 2026) where a model heading like
    "Haiku 5.5" is followed on subsequent lines by "Input", "$0.10", "/ MTok",
    "Output", "$0.50", "/ MTok", etc. Some models have tiered pricing with
    "Prompts ≤ 100K tokens" / "Prompts > 100K tokens" subsections; both
    tiers are retained only when their prices and thresholds are complete.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

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
_LABELS = {"input", "output", "read", "write"}
_PRICE_VALUE_RE = re.compile(r"\$[0-9]+(?:\.[0-9]+)?")
_COMPARISON = r"(?:≤|<=|<|>|>=|≥|\bup to|\bover|\babove|\bunder|\bmore than|\bfewer than)"
_CONTEXT_COUNT = r"(?:[0-9]+(?:\.[0-9]+)?\s*[KM]\b(?:\s+tokens\b)?|[0-9]+\s+tokens\b)"
_TIER_TEXT_RE = re.compile(
    rf"^Prompts\b|{_COMPARISON}\s*{_CONTEXT_COUNT}|{_CONTEXT_COUNT}\s*{_COMPARISON}",
    re.IGNORECASE,
)
# Longest threshold wording treated as a tier heading wherever it appears.
_TIER_HEADING_MAX_WORDS = 6
_TIER_HEADING_RE = re.compile(
    r"Prompts (?P<side>≤|<=|>) (?P<count>[0-9]+)(?P<unit>K|M) tokens"
)


def _is_model_name(name: str) -> tuple[str, str] | None:
    parts = name.split()
    if len(parts) == 2 and parts[0] in _MODEL_FAMILIES and parts[1].replace(".", "", 1).isdigit():
        return parts[0], parts[1]
    return None


def _structured_cards(soup: BeautifulSoup) -> list[tuple[str, Tag]]:
    cards = []
    for heading in soup.select('h3.card_pricing_title_text, h3[class*="__modelName"]'):
        parsed = _is_model_name(heading.get_text(strip=True))
        card = heading.parent.parent if heading.parent else None
        if parsed is not None and isinstance(card, Tag):
            family, version = parsed
            cards.append((f"anthropic/claude-{family.lower()}-{version}", card))
    return cards


def _parse_structured(soup: BeautifulSoup) -> dict:
    """Parse the Webflow / CSS-module layouts where each model lives in a card."""
    out: dict = {}
    for or_id, card in _structured_cards(soup):
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


def _collect_lines(soup: Tag) -> list[str]:
    """Return a flat list of non-empty text lines from the page."""
    lines: list[str] = []
    for s in soup.stripped_strings:
        text = s.strip()
        if text:
            lines.append(text)
    return lines


def _parse_tiered_card(segment: list[str]) -> dict | None:
    """Require matching input/output tiers and optional tier-scoped cache prices."""
    threshold: int | None = None
    section = ""
    headings: dict[str, list[int]] = {}
    prices: dict[tuple[str, int], int] = {}
    for i, line in enumerate(segment):
        label = line.lower()
        if label in {"prompt caching", "input", "output"}:
            section = label
            if section in headings:
                return None
            headings[section] = []
            continue

        price_key: str | None = None
        if line.startswith("Prompts"):
            match = _TIER_HEADING_RE.fullmatch(line)
            if match is None or not section:
                return None
            bound = int(match.group("count")) * (
                1_000 if match.group("unit") == "K" else 1_000_000
            )
            if bound <= 0 or (threshold is not None and threshold != bound):
                return None
            threshold = bound
            tier = 1 if match.group("side") == ">" else 0
            seen = headings[section]
            if seen and tier <= seen[-1]:
                return None
            # Input/output require low then high. Cache quotes may name only
            # one tier, since Read is optional independently for each tier.
            if section != "prompt caching" and tier != len(seen):
                return None
            seen.append(tier)
            if section in {"input", "output"}:
                price_key = section
        elif label in {"read", "write"}:
            if section != "prompt caching" or not headings[section]:
                return None
            price_key = label

        if price_key is not None:
            key = (price_key, headings[section][-1])
            if key in prices or i + 1 >= len(segment):
                return None
            price_match = _PRICE_RE.fullmatch(segment[i + 1])
            if price_match is None:
                return None
            price = Decimal(price_match.group(1))
            if price <= 0:
                return None
            prices[key] = int(price * 1_000_000)

    if threshold is None or any(headings.get(label) != [0, 1] for label in ("input", "output")):
        return None
    # Cache reads are optional, but a one-sided quote must not imply a cache
    # discount for the other tier. Omit cached prices from BOTH tiers in that case.
    has_cache = all(("read", tier) in prices for tier in (0, 1))
    tiers = []
    for tier in (0, 1):
        row = {
            "max_prompt_tokens": threshold if tier == 0 else None,
            "prompt_micro_per_m": prices[("input", tier)],
            "completion_micro_per_m": prices[("output", tier)],
        }
        if has_cache:
            row["prompt_cached_micro_per_m"] = prices[("read", tier)]
        tiers.append(row)
    return {"tiers": tiers}


def _is_tier_heading(text: str) -> bool:
    """A threshold that reads as a heading, not as a sentence about the model.

    "Up to 200K tokens" names a price tier. "Our fastest model, with up to 1M
    tokens of context" describes the model and says nothing about its prices,
    so a description must not make an ordinary card lose its row.
    """
    return _TIER_TEXT_RE.search(text) is not None and (
        text.lower().startswith("prompts") or len(text.split()) <= _TIER_HEADING_MAX_WORDS
    )


def _multi_priced(lines: list[str]) -> bool:
    """Evidence only: accepting tier prices remains _parse_tiered_card's job."""
    # Threshold wording counts anywhere when it reads as a heading, and
    # anywhere inside the price area, which starts at the first quantity label.
    price_area = next(
        (i for i, line in enumerate(lines) if line.lower() in _LABELS | {"prompt caching"}),
        len(lines),
    )
    # Inline markup can split a comparison/count over several text nodes.
    if _TIER_TEXT_RE.search(" ".join(lines[price_area:])):
        return True
    seen: set[str] = set()
    label = ""
    values = 0
    for index, line in enumerate(lines):
        if _is_tier_heading(line) or (index >= price_area and _TIER_TEXT_RE.search(line)):
            return True
        if line.lower() in _LABELS:
            label = line.lower()
            if label in seen:
                return True
            seen.add(label)
            values = 0
        elif label:
            values += len(_PRICE_VALUE_RE.findall(line))
            if values > 1:
                return True
    return False


def _flat_cards(lines: list[str], *, bounded: bool = True) -> list[tuple[str, list[str]]]:
    positions = [(i, match) for i, line in enumerate(lines)
                 if (match := _MODEL_HEADING_RE.fullmatch(line)) is not None]
    cards = []
    for index, (start, match) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(lines)
        segment = lines[start + 1:end]
        # Flat dumps lack a DOM boundary. After both input and output have
        # prices, stop at the first non-card line. Labels, dollar amounts,
        # units, context headings and split threshold fragments belong to a
        # card; prose/FAQ/banner text does not. Never inspect the page tail
        # for duplicate labels or dollar amounts beyond this boundary.
        priced: set[str] = set()
        label = ""
        for i, line in enumerate(segment):
            lower = line.lower()
            card_line = (
                lower in _LABELS | {"prompt caching"}
                or _PRICE_VALUE_RE.search(line) is not None
                or _TIER_TEXT_RE.search(line) is not None
                or re.fullmatch(_COMPARISON, line, re.I) is not None
                or re.fullmatch(r"/\s*MTok", line, re.I) is not None
                or re.fullmatch(r"(?:short|long|standard|extended) (?:context|prompts)", lower)
                or re.fullmatch(
                    r"(?:short|long|standard|extended|context|prompts|tokens|up|to|over|above|"
                    r"under|more|fewer|than|[≤<>≥=]+|[0-9.]+[KM]?)", line, re.I,
                )
            )
            if bounded and {"input", "output"} <= priced and not card_line:
                segment = segment[:i]
                break
            if lower in _LABELS:
                label = lower
            elif label and _PRICE_VALUE_RE.search(line):
                priced.add(label)
        model_id = f"anthropic/claude-{match.group('family').lower()}-{match.group('version')}"
        cards.append((model_id, segment))
    return cards


def _structured_multi_priced(cards: list[tuple[str, Tag]]) -> set[str]:
    models = set()
    for model_id, card in cards:
        # Use the structured reader's DOM boundary, not the page tail.
        # Joined element text catches thresholds split by inline markup;
        # the model heading is never evidence of a pricing threshold.
        lines = []
        for node in card.descendants:
            if isinstance(node, Tag):
                raw_value = node.get("data-value")
                if (isinstance(raw_value, str)
                        and node.select_one('.tokens_main_val_number, [class*="__priceValue"]') is None
                        and any("tokens_main_val_number" == name or "__priceValue" in name
                                for name in node.get("class", []))
                        and "$" not in node.get_text()):
                    lines.append(f"${raw_value}")
            elif isinstance(node, NavigableString) and not isinstance(node, Comment):
                text = node.strip()
                if text and not _MODEL_HEADING_RE.fullmatch(text):
                    lines.append(text)
        tier_text = any(
            _is_tier_heading(node.get_text(" ", strip=True))
            for node in card.find_all(True) if node.name != "h3"
        )
        labels = [node.get_text(" ", strip=True)
                  for node in card.select('.tokens_main_label, [class*="__priceLabel"]')]
        multiple_values = any(
            len(node.select('.tokens_main_val_number, [class*="__priceValue"]')) > 1
            for node in card.select('.tokens_main_wrap, [class*="__priceRow"]')
            if (label := node.select_one('.tokens_main_label, [class*="__priceLabel"]'))
            and label.get_text(" ", strip=True).lower() in _LABELS
        )
        if tier_text or multiple_values or _multi_priced(lines) or _multi_priced(labels):
            models.add(model_id)
    return models


def _parse_flat_text(cards: list[tuple[str, list[str]]]) -> dict:
    """Parse the flat-text dump format.

    After a line matching e.g. "Haiku 5.5", scan forward until the next model
    heading, looking for label lines "Input"/"Output"/"Read"/"Write" each
    followed (within a few lines) by a "$X.YZ" line. Tiered cards instead
    require two complete, consistent tiers and never fall back to flat prices.
    """
    out: dict = {}
    for or_id, segment in cards:
        if or_id in out:
            continue
        if any(line.startswith("Prompts") for line in segment):
            tiered = _parse_tiered_card(segment)
            if tiered is not None:
                out[or_id] = tiered
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

    # Prefer actual DOM card boundaries when available, including for the
    # flat reader. A complete tier breakdown beats a structured headline.
    lines = _collect_lines(soup)
    structured_cards = _structured_cards(soup)
    structured_ids = {model_id for model_id, _card in structured_cards}
    cards = [(model_id, segment) for model_id, segment in _flat_cards(lines)
             if model_id not in structured_ids]
    for _model_id, card in structured_cards:
        cards.extend(_flat_cards(_collect_lines(card), bounded=False))
    flat = _parse_flat_text(cards)
    for key, value in flat.items():
        if key not in out or "tiers" in value:
            out[key] = value

    # Enforce the same fail-closed rule once, after merging both readers and
    # before deriving Fast SKUs, so a rejected base cannot leak a flat variant.
    multi_priced = _structured_multi_priced(structured_cards)
    multi_priced.update(model_id for model_id, segment in cards
                        if _multi_priced(segment))
    for model_id in multi_priced:
        if model_id in out and "tiers" not in out[model_id]:
            del out[model_id]

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
        if "tiers" in base:
            out[f"{base_id}-fast"] = {"tiers": [
                {
                    key: value if key == "max_prompt_tokens" else int(Decimal(value) * multiplier)
                    for key, value in tier.items()
                }
                for tier in base["tiers"]
            ]}
        else:
            out[f"{base_id}-fast"] = {
                key: int(Decimal(value) * multiplier) for key, value in base.items()
            }
    return out
