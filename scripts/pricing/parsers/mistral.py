# LLM-MAINTAINED FILE — re-validated every hour by scripts/pricing/refresh.py.
# Initial version derived from a real fetch of mistral.ai/pricing on
# 2026-05-08. Captured fixture lives at tests/fixtures/pricing/mistral.html.
#
# Page structure: Mistral's pricing page is a Next.js app with the model
# list embedded as JSON in <script> tags. Each model object has shape:
#   {"name": "Devstral 2", "api_endpoint": "devstral-medium-latest",
#    "price": [{"value": "Input (/M tokens)", "price_dollar": "<p>$0.4</p>"},
#              {"value": "Output (/M tokens)", "price_dollar": "<p>$2</p>"}]}
# We extract input + output from the price array and pair them.
"""Mistral pricing-page parser."""

from __future__ import annotations

import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from bs4 import BeautifulSoup, Tag

# Display name on mistral.ai → OR-canonical id. Extended over time.
_NAME_TO_OR_ID = {
    "Mistral Medium 3.5": "mistralai/mistral-medium-3-5",
    "Mistral Medium 3.1": "mistralai/mistral-medium-3.1",
    "Mistral Medium 3": "mistralai/mistral-medium-3",
    "Mistral Small 4": "mistralai/mistral-small-2603",
    "Mistral Small 3.2": "mistralai/mistral-small-3.2-24b-instruct",
    "Mistral Large 3": "mistralai/mistral-large",
    "Devstral 2": "mistralai/devstral-medium",
    "Devstral Small 2": "mistralai/devstral-small",
    "Magistral Medium": "mistralai/magistral-medium",
    "Magistral Small": "mistralai/magistral-small",
    "Ministral 3 - 3B": "mistralai/ministral-3b-2512",
    "Ministral 3 - 8B": "mistralai/ministral-8b-2512",
    "Ministral 3 - 14B": "mistralai/ministral-14b-2512",
    "Codestral": "mistralai/codestral-2508",
    "Pixtral Large": "mistralai/pixtral-large-2411",
    "Mixtral 8x22B": "mistralai/mixtral-8x22b-instruct",
    "Mistral NeMo": "mistralai/mistral-nemo",
}


# `<p>$0.4</p>` or `<p>$0.4</p>` — JSON-escaped HTML.
_DOLLAR_RE = re.compile(r"\$([\d.]+)")


def _to_micro_per_m(text: str | None) -> int | None:
    if not text:
        return None
    match = _DOLLAR_RE.search(text)
    if not match:
        return None
    try:
        value = Decimal(match.group(1)) * Decimal(1_000_000)
        return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    except (InvalidOperation, TypeError, ValueError):
        return None


def _parse_embedded_json(html: str) -> dict:
    out: dict = {}
    # Mistral embeds the model JSON inside a Next.js __next_f payload as
    # a string literal, so the JSON quote characters are escaped:
    # `\"name\":\"Devstral 2\"` rather than `"name":"Devstral 2"`. We
    # un-escape a working copy first so the rest of the parsing reads
    # like normal JSON.
    text = html.replace('\\"', '"')

    # For every model-name occurrence, look ahead ~4KB for the price
    # array containing Input and Output entries.
    name_re = re.compile(r'"name"\s*:\s*"([^"]+)"')
    for m in name_re.finditer(text):
        name = m.group(1)
        or_id = _NAME_TO_OR_ID.get(name)
        if or_id is None:
            continue
        # Skip duplicate occurrences (Mistral references each model in
        # nav menus and footer; only the pricing JSON has a `price` array).
        if or_id in out:
            continue
        window = text[m.end() : m.end() + 4000]
        price_match = re.search(r'"price"\s*:\s*\[(.*?)\]', window, re.DOTALL)
        if not price_match:
            continue
        prices_block = price_match.group(1)
        input_match = re.search(
            r'"value"\s*:\s*"Input[^"]*"[^}]*?"price_dollar"\s*:\s*"([^"]+)"',
            prices_block,
            re.DOTALL,
        )
        output_match = re.search(
            r'"value"\s*:\s*"Output[^"]*"[^}]*?"price_dollar"\s*:\s*"([^"]+)"',
            prices_block,
            re.DOTALL,
        )
        if not input_match or not output_match:
            continue
        prompt = _to_micro_per_m(input_match.group(1))
        completion = _to_micro_per_m(output_match.group(1))
        if prompt is None or completion is None:
            continue
        out[or_id] = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
    return out


def _rendered_price(card: Tag, label_prefix: str) -> int | None:
    for label in card.find_all("p"):
        if not isinstance(label, Tag):
            continue
        if not label.get_text(" ", strip=True).startswith(label_prefix):
            continue
        row = label.parent
        if not isinstance(row, Tag):
            continue
        price = row.find("mistral-atom-text-price")
        if isinstance(price, Tag):
            return _to_micro_per_m(price.get_text(" ", strip=True))
    return None


def _parse_rendered_cards(html: str) -> dict:
    """Parse the server-rendered cards on Mistral's dedicated API page."""
    out: dict = {}
    soup = BeautifulSoup(html, "html.parser")
    for name_node in soup.find_all("p"):
        if not isinstance(name_node, Tag):
            continue
        name = name_node.get_text(" ", strip=True)
        or_id = _NAME_TO_OR_ID.get(name)
        if or_id is None or or_id in out:
            continue

        # Find the smallest enclosing card that contains both token-price
        # rows. The model name also appears in navigation and featured-model
        # links, so anchoring to those rows avoids crossing into another card.
        card = name_node.parent
        while isinstance(card, Tag):
            recognized_names = {
                node.get_text(" ", strip=True)
                for node in card.find_all("p")
                if isinstance(node, Tag) and node.get_text(" ", strip=True) in _NAME_TO_OR_ID
            }
            if len(recognized_names) > 1:
                # Navigation/page-root containers span multiple models. Any
                # larger ancestor will too, so this name is not a price-card
                # anchor and must not inherit a neighboring model's prices.
                break
            prompt = _rendered_price(card, "Input (/M tokens)")
            completion = _rendered_price(card, "Output (/M tokens)")
            if prompt is not None and completion is not None:
                out[or_id] = {
                    "prompt_micro_per_m": prompt,
                    "completion_micro_per_m": completion,
                }
                break
            card = card.parent
    return out


def parse(html: str) -> dict:
    # Support both the older embedded Next.js payload and the current
    # server-rendered API cards. Current visible cards win if both exist.
    out = _parse_embedded_json(html)
    out.update(_parse_rendered_cards(html))
    return out
