# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses the server-rendered HTML at ai.google.dev/gemini-api/docs/pricing.
# The parser also accepts the historical normalized Markdown format so captured
# fixtures remain useful during the migration away from that mirror.
# Captured fixture lives at tests/fixtures/pricing/gemini.html.
#
# Page format: each model lives under a `## Gemini X.Y Flavor` heading,
# followed by tables shaped:
#
#   |  | Free Tier | Paid Tier, per 1M tokens in USD |
#   | --- | --- | --- |
#   | Input price | Free of charge | $1.25, prompts <= 200k tokens $2.50, prompts > 200k tokens |
#   | Output price (including thinking tokens) | Free of charge | $10.00, prompts <= 200k tokens $15.00, prompts > 200k |
#
# We extract the Paid Tier column. When a price line carries
# "<= 200k / > 200k" markers, we emit two PriceTier rows; otherwise
# a single uncapped tier.
"""Google Gemini pricing parser for official HTML and legacy Markdown."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, Tag

# Markdown heading text → OR-canonical id.
_NAME_TO_OR_ID = {
    "Gemini 2.5 Pro": "google/gemini-2.5-pro",
    "Gemini 2.5 Flash": "google/gemini-2.5-flash",
    "Gemini 2.5 Flash-Lite": "google/gemini-2.5-flash-lite",
    "Gemini 2.0 Flash": "google/gemini-2.0-flash-001",
    "Gemini 2.0 Flash-Lite": "google/gemini-2.0-flash-lite-001",
    "Gemini 3.1 Pro Preview": "google/gemini-3.1-pro-preview",
    "Gemini 3.5 Flash": "google/gemini-3.5-flash",
    "Gemini 3.6 Flash": "google/gemini-3.6-flash",
    "Gemini 3.1 Flash-Lite": "google/gemini-3.1-flash-lite",
    "Gemini 3.1 Flash-Lite Preview": "google/gemini-3.1-flash-lite-preview",
    "Gemini 3 Flash Preview": "google/gemini-3-flash-preview",
    "Gemini 1.5 Pro": "google/gemini-1.5-pro",
    "Gemini 1.5 Flash": "google/gemini-1.5-flash",
}

_STANDARD_MODEL_HEADING_RE = re.compile(
    r"^Gemini\s+(?P<version>\d+(?:\.\d+)?)\s+"
    r"(?P<variant>Pro|Flash(?:[ -]Lite)?)$",
    re.IGNORECASE,
)


def _model_id_from_heading(heading: str) -> str | None:
    """Map stable Gemini pricing headings without a per-release code edit."""

    mapped = _NAME_TO_OR_ID.get(heading)
    if mapped is not None:
        return mapped
    match = _STANDARD_MODEL_HEADING_RE.fullmatch(heading.strip())
    if match is None:
        return None
    variant = match.group("variant").casefold().replace(" ", "-")
    return f"google/gemini-{match.group('version')}-{variant}"


# "$1.25, prompts <= 200k tokens $2.50, prompts > 200k tokens" — a
# tiered price line with an explicit threshold.
_TIERED_PRICE_RE = re.compile(
    r"\$([\d.]+)\s*,?\s*prompts?\s*(?:<=?|≤)\s*([\d.,]+)\s*([kKmM])?\s*tokens?"
    r".*?\$([\d.]+)\s*,?\s*prompts?\s*>\s*[\d.,]+\s*[kKmM]?",
    re.IGNORECASE | re.DOTALL,
)

_ANY_DOLLAR_RE = re.compile(r"\$([\d.]+)")


def _parse_threshold(value: str, unit: str | None) -> int:
    """Convert 200, 200k, 1M etc. to an integer token count."""
    cleaned = value.replace(",", "")
    try:
        n = float(cleaned)
    except ValueError:
        return 0
    multiplier = 1
    if unit:
        if unit.lower() == "k":
            multiplier = 1_000
        elif unit.lower() == "m":
            multiplier = 1_000_000
    return int(n * multiplier)


def _to_micro_per_m(usd: str) -> int:
    return int(round(float(usd) * 1_000_000))


def _parse_price_cell(text: str) -> tuple[list[tuple[int | None, int]], bool]:
    """Parse a Gemini "Paid Tier" cell. Returns (tier_list, was_tiered).

    `tier_list` is a list of (max_prompt_tokens, micro_per_m) pairs;
    the last entry has max_prompt_tokens=None when tiered, or is the
    only entry when flat.
    """
    tiered_match = _TIERED_PRICE_RE.search(text)
    if tiered_match:
        low_usd, threshold_value, threshold_unit, high_usd = tiered_match.groups()
        threshold = _parse_threshold(threshold_value, threshold_unit)
        return (
            [
                (threshold, _to_micro_per_m(low_usd)),
                (None, _to_micro_per_m(high_usd)),
            ],
            True,
        )
    # Flat: take the first $-amount in the cell. Skip cells that say
    # "Free of charge" or "Not available".
    lowered = text.casefold()
    if "free" in lowered or "not available" in lowered:
        return [], False
    flat_match = _ANY_DOLLAR_RE.search(text)
    if flat_match:
        return [(None, _to_micro_per_m(flat_match.group(1)))], False
    return [], False


def _price_from_rows(rows: list[tuple[str, str]]) -> dict[str, object] | None:
    """Build one model price from label/paid-tier cell pairs."""

    prompt_tiers: list[tuple[int | None, int]] = []
    completion_tiers: list[tuple[int | None, int]] = []
    cached_tiers: list[tuple[int | None, int]] = []
    for raw_label, paid_cell in rows:
        label = raw_label.casefold()
        if "input price" in label and not prompt_tiers:
            prompt_tiers, _ = _parse_price_cell(paid_cell)
        elif "output price" in label and not completion_tiers:
            completion_tiers, _ = _parse_price_cell(paid_cell)
        elif "context caching price" in label and not cached_tiers:
            cached_tiers, _ = _parse_price_cell(paid_cell)
    if not prompt_tiers or not completion_tiers:
        return None

    # Gemini tiers prompt and completion at the same thresholds. If the page
    # ever diverges, keep billing conservative and use the low flat tier until
    # the hourly parser review adapts the richer shape.
    if len(prompt_tiers) != len(completion_tiers) or any(
        prompt[0] != completion[0]
        for prompt, completion in zip(prompt_tiers, completion_tiers, strict=False)
    ):
        return {
            "prompt_micro_per_m": prompt_tiers[0][1],
            "completion_micro_per_m": completion_tiers[0][1],
        }

    tiers: list[dict[str, int | None]] = []
    for index, ((threshold, prompt_micro), (_threshold, completion_micro)) in enumerate(
        zip(prompt_tiers, completion_tiers, strict=False)
    ):
        tier: dict[str, int | None] = {
            "max_prompt_tokens": threshold,
            "prompt_micro_per_m": prompt_micro,
            "completion_micro_per_m": completion_micro,
        }
        if len(cached_tiers) == len(prompt_tiers):
            cached_threshold, cached_micro = cached_tiers[index]
            if cached_threshold == threshold:
                tier["prompt_cached_micro_per_m"] = cached_micro
        tiers.append(tier)

    if len(tiers) > 1:
        return {"tiers": tiers}
    only = tiers[0]
    price: dict[str, object] = {
        "prompt_micro_per_m": only["prompt_micro_per_m"],
        "completion_micro_per_m": only["completion_micro_per_m"],
    }
    if "prompt_cached_micro_per_m" in only:
        price["prompt_cached_micro_per_m"] = only["prompt_cached_micro_per_m"]
    return price


def _parse_markdown(md: str) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    # Walk every "## Heading" and the first (Standard) table under it.
    sections = re.split(r"(?m)^## ", md)
    for section in sections:
        if not section:
            continue
        # The first line after split is the heading; everything else
        # belongs to that section until the next "##".
        first_newline = section.find("\n")
        if first_newline == -1:
            continue
        heading = section[:first_newline].strip()
        or_id = _model_id_from_heading(heading)
        if or_id is None:
            continue
        body = section[first_newline + 1 :]
        rows: list[tuple[str, str]] = []
        for line in body.splitlines():
            if not line.startswith("|"):
                if rows:
                    break
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 3:
                continue
            rows.append((cells[0], cells[-1]))
        price = _price_from_rows(rows)
        if price is not None:
            out[or_id] = price
    return out


def _standard_table(heading: Tag) -> Tag | None:
    """Return the official page's Standard table before the next model."""

    in_standard_section = False
    for element in heading.find_all_next(["h2", "h3", "table"]):
        if not isinstance(element, Tag):
            continue
        if element.name == "h2":
            return None
        if element.name == "h3":
            in_standard_section = element.get_text(" ", strip=True).casefold() == "standard"
            continue
        if element.name == "table" and in_standard_section:
            return element
    return None


def _parse_html(html: str) -> dict[str, dict[str, object]]:
    out: dict[str, dict[str, object]] = {}
    soup = BeautifulSoup(html, "html.parser")
    for heading in soup.find_all("h2"):
        if not isinstance(heading, Tag):
            continue
        or_id = _model_id_from_heading(heading.get_text(" ", strip=True))
        if or_id is None:
            continue
        table = _standard_table(heading)
        if table is None:
            continue
        rows: list[tuple[str, str]] = []
        for row in table.find_all("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) < 3:
                continue
            rows.append(
                (
                    cells[0].get_text(" ", strip=True),
                    cells[-1].get_text(" ", strip=True),
                )
            )
        price = _price_from_rows(rows)
        if price is not None:
            out[or_id] = price
    return out


def parse(content: str) -> dict[str, dict[str, object]]:
    """Parse Gemini pricing from the official HTML or a legacy Markdown copy."""

    if re.search(r"<h2\b", content, re.IGNORECASE) and re.search(
        r"<table\b", content, re.IGNORECASE
    ):
        return _parse_html(content)
    return _parse_markdown(content)
