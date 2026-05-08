# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
#
# Parses ai.google.dev/gemini-api/docs/pricing as rendered by r.jina.ai.
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
"""Google Gemini pricing parser (Jina-rendered markdown)."""
from __future__ import annotations

import re

# Markdown heading text → OR-canonical id.
_NAME_TO_OR_ID = {
    "Gemini 2.5 Pro": "google/gemini-2.5-pro",
    "Gemini 2.5 Flash": "google/gemini-2.5-flash",
    "Gemini 2.5 Flash-Lite": "google/gemini-2.5-flash-lite",
    "Gemini 2.0 Flash": "google/gemini-2.0-flash-001",
    "Gemini 2.0 Flash-Lite": "google/gemini-2.0-flash-lite-001",
    "Gemini 3.1 Pro Preview": "google/gemini-3.1-pro-preview",
    "Gemini 3.1 Flash-Lite": "google/gemini-3.1-flash-lite",
    "Gemini 3.1 Flash-Lite Preview": "google/gemini-3.1-flash-lite-preview",
    "Gemini 3 Flash Preview": "google/gemini-3-flash-preview",
    "Gemini 1.5 Pro": "google/gemini-1.5-pro",
    "Gemini 1.5 Flash": "google/gemini-1.5-flash",
}


# "$1.25, prompts <= 200k tokens $2.50, prompts > 200k tokens" — a
# tiered price line with an explicit threshold.
_TIERED_PRICE_RE = re.compile(
    r"\$([\d.]+)\s*,?\s*prompts?\s*(?:<=?|≤)\s*([\d.,]+)\s*([kKmM])?\s*tokens?"
    r".*?\$([\d.]+)\s*,?\s*prompts?\s*>\s*[\d.,]+\s*[kKmM]?",
    re.IGNORECASE | re.DOTALL,
)

# "$0.30 (text / image / video) $1.00 (audio)" or just "$0.30" — a
# flat price (we take the first text/image/video price for chat).
_FLAT_TEXT_PRICE_RE = re.compile(
    r"\$([\d.]+)(?:\s*\(text|\s*$|\s*[a-zA-Z]?[^,$])"
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
    if "Free" in text or "Not available" in text:
        return [], False
    flat_match = _ANY_DOLLAR_RE.search(text)
    if flat_match:
        return [(None, _to_micro_per_m(flat_match.group(1)))], False
    return [], False


def parse(md: str) -> dict:
    out: dict = {}
    # Walk every "## Heading" + the table directly under it.
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
        or_id = _NAME_TO_OR_ID.get(heading)
        if or_id is None:
            continue
        body = section[first_newline + 1 :]
        # Find the first table block (Standard tier — Jina puts the
        # Standard table first under the heading).
        prompt_tiers: list[tuple[int | None, int]] = []
        completion_tiers: list[tuple[int | None, int]] = []
        for line in body.splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 3:
                continue
            label = cells[0].lower()
            paid_cell = cells[-1]  # Last column is "Paid Tier, per 1M tokens"
            if "input price" in label and not prompt_tiers:
                tiers, _ = _parse_price_cell(paid_cell)
                prompt_tiers = tiers
            elif "output price" in label and not completion_tiers:
                tiers, _ = _parse_price_cell(paid_cell)
                completion_tiers = tiers
            if prompt_tiers and completion_tiers:
                break
        if not prompt_tiers or not completion_tiers:
            continue
        # Pair up the tiers. They should have matching length + thresholds
        # (Gemini tiers prompt and completion the same way). If they
        # don't match, fall back to flat using the cheapest.
        if len(prompt_tiers) == len(completion_tiers) and all(
            p[0] == c[0]
            for p, c in zip(prompt_tiers, completion_tiers, strict=False)
        ):
            tiers = []
            for (threshold, prompt_micro), (_t2, completion_micro) in zip(
                prompt_tiers, completion_tiers, strict=False
            ):
                tiers.append(
                    {
                        "max_prompt_tokens": threshold,
                        "prompt_micro_per_m": prompt_micro,
                        "completion_micro_per_m": completion_micro,
                    }
                )
            if len(tiers) > 1:
                out[or_id] = {"tiers": tiers}
            else:
                t = tiers[0]
                out[or_id] = {
                    "prompt_micro_per_m": t["prompt_micro_per_m"],
                    "completion_micro_per_m": t["completion_micro_per_m"],
                }
        else:
            # Tier shape mismatch — fall back to flat (low tier).
            out[or_id] = {
                "prompt_micro_per_m": prompt_tiers[0][1],
                "completion_micro_per_m": completion_tiers[0][1],
            }
    return out
