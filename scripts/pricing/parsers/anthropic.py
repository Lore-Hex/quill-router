# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
# Initial version derived from a real fetch of www.anthropic.com/pricing on
# 2026-05-08. Captured fixture lives at tests/fixtures/pricing/anthropic.html
# and is the ground truth that tests/test_pricing_fixtures.py runs against.
#
# Page structure (as of capture):
#   * Each model is an <h3 class="card_pricing_title_text"> with text like
#     "Opus 4.7", "Sonnet 4.6", "Haiku 4.5".
#   * Walking up two ancestors from the h3 lands on the model card.
#   * Inside each card, four <span class="tokens_main_val_number" data-value="N">
#     elements appear in order: Input, Output, Cache Write, Cache Read.
#   * We use the first two (Input, Output) for prompt/completion pricing.
"""Anthropic pricing-page parser."""
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


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}
    for heading in soup.select("h3.card_pricing_title_text"):
        name = heading.get_text(strip=True)
        parts = name.split()
        if (
            len(parts) != 2
            or parts[0] not in _MODEL_FAMILIES
            or not parts[1].replace(".", "", 1).isdigit()
        ):
            continue
        or_id = f"anthropic/claude-{parts[0].lower()}-{parts[1]}"
        # The card container is two levels up from the heading.
        card = heading.parent.parent if heading.parent else None
        if card is None:
            continue
        # First two .tokens_main_val_number values are Input / Output in $/MTok.
        spans = card.select(".tokens_main_val_number")
        if len(spans) < 2:
            continue
        try:
            prompt_usd = Decimal(spans[0].get("data-value") or spans[0].text)
            completion_usd = Decimal(spans[1].get("data-value") or spans[1].text)
            cache_read_usd = (
                Decimal(spans[3].get("data-value") or spans[3].text)
                if len(spans) >= 4
                else None
            )
        except (InvalidOperation, TypeError, ValueError):
            continue
        row = {
            "prompt_micro_per_m": int(prompt_usd * 1_000_000),
            "completion_micro_per_m": int(completion_usd * 1_000_000),
        }
        if cache_read_usd is not None:
            row["prompt_cached_micro_per_m"] = int(cache_read_usd * 1_000_000)
        out[or_id] = row

    # Anthropic documents Fast mode as a multiplier below the standard model
    # cards rather than rendering a second pricing card. Publish the distinct
    # `-fast` SKU only when that statement and its base card are both present.
    page_text = " ".join(soup.stripped_strings)
    for match in _FAST_MODE_RE.finditer(page_text):
        family = match.group("family").lower()
        version = match.group("version")
        base_id = f"anthropic/claude-{family}-{version}"
        base = out.get(base_id)
        if base is None:
            continue
        multiplier = Decimal(match.group("multiplier"))
        out[f"{base_id}-fast"] = {
            key: int(Decimal(value) * multiplier)
            for key, value in base.items()
        }
    return out
