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

from bs4 import BeautifulSoup

# Display name on anthropic.com → OR-canonical id. Extended over time.
_NAME_TO_OR_ID = {
    "Opus 4.7": "anthropic/claude-opus-4.7",
    "Sonnet 4.6": "anthropic/claude-sonnet-4.6",
    "Haiku 4.5": "anthropic/claude-haiku-4.5",
    "Opus 4.6": "anthropic/claude-opus-4.6",
    "Sonnet 4.5": "anthropic/claude-sonnet-4.5",
    "Opus 4.5": "anthropic/claude-opus-4.5",
    "Opus 4.1": "anthropic/claude-opus-4.1",
    "Sonnet 4": "anthropic/claude-sonnet-4",
    "Opus 4": "anthropic/claude-opus-4",
    "Haiku 4": "anthropic/claude-haiku-4",
}


def parse(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    out: dict = {}
    for heading in soup.select("h3.card_pricing_title_text"):
        name = heading.get_text(strip=True)
        or_id = _NAME_TO_OR_ID.get(name)
        if or_id is None:
            continue
        # The card container is two levels up from the heading.
        card = heading.parent.parent if heading.parent else None
        if card is None:
            continue
        # First two .tokens_main_val_number values are Input / Output in $/MTok.
        spans = card.select(".tokens_main_val_number")
        if len(spans) < 2:
            continue
        try:
            prompt_usd = float(spans[0].get("data-value") or spans[0].text)
            completion_usd = float(spans[1].get("data-value") or spans[1].text)
        except (TypeError, ValueError):
            continue
        out[or_id] = {
            "prompt_micro_per_m": int(round(prompt_usd * 1_000_000)),
            "completion_micro_per_m": int(round(completion_usd * 1_000_000)),
        }
    return out
