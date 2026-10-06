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
    "Ministral 3 3B": "mistralai/ministral-3b-2512",
    "Ministral 3 8B": "mistralai/ministral-8b-2512",
    "Ministral 3 14B": "mistralai/ministral-14b-2512",
    "Codestral": "mistralai/codestral-2508",
    "Pixtral Large": "mistralai/pixtral-large-2411",
    "Mixtral 8x22B": "mistralai/mixtral-8x22b-instruct",
    "Mistral NeMo": "mistralai/mistral-nemo",
}


# `<p>$0.4</p>` or `<p>$0.4</p>` — JSON-escaped HTML.
_DOLLAR_RE = re.compile(r"\$([\d.]+)")


def _model_id(name: str) -> str | None:
    if name in _NAME_TO_OR_ID:
        return _NAME_TO_OR_ID[name]
    # New numbered first-party chat families still need an exact match in
    # the authenticated catalog before discovery can publish a route.
    if re.fullmatch(r"(?:Mistral|Ministral|Devstral|Magistral|Pixtral) [A-Za-z0-9 .-]+", name):
        slug = re.sub(r"[ .]+", "-", name.casefold())
        return f"mistralai/{slug}"
    return None


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
        or_id = _model_id(name)
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
        row = {
            "prompt_micro_per_m": prompt,
            "completion_micro_per_m": completion,
        }
        cached_match = re.search(
            r'"value"\s*:\s*"Cached input[^"]*"[^}]*?"price_dollar"\s*:\s*"([^"]+)"',
            prices_block,
            re.DOTALL,
        )
        if cached_match:
            cached = _to_micro_per_m(cached_match.group(1))
            if cached is not None:
                row["prompt_cached_micro_per_m"] = cached
        out[or_id] = row
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
        or_id = _model_id(name)
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
                if isinstance(node, Tag) and _model_id(node.get_text(" ", strip=True))
            }
            if len(recognized_names) > 1:
                # Navigation/page-root containers span multiple models. Any
                # larger ancestor will too, so this name is not a price-card
                # anchor and must not inherit a neighboring model's prices.
                break
            prompt = _rendered_price(card, "Input (/M tokens)")
            completion = _rendered_price(card, "Output (/M tokens)")
            if prompt is not None and completion is not None:
                row = {
                    "prompt_micro_per_m": prompt,
                    "completion_micro_per_m": completion,
                }
                # Mistral prices cache hits at a flat -90%, and some cards
                # carry the explicit "Cached input (/M tokens)" row. Emit it
                # when present; the settle-time mistral fallback multiplier
                # covers cards that omit it.
                cached = _rendered_price(card, "Cached input (/M tokens)")
                if cached is not None:
                    row["prompt_cached_micro_per_m"] = cached
                out[or_id] = row
                break
            card = card.parent
    return out


def _parse_tables(html: str) -> dict:
    """Read the current docs' standard USD/M-token tables by column label."""
    out: dict = {}
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        if not isinstance(table, Tag):
            continue
        if any(
            node.has_attr("hidden")
            or node.get("aria-hidden") == "true"
            or node.get("data-state") == "inactive"
            for node in [table, *table.parents]
            if isinstance(node, Tag)
        ):
            continue
        headers = [node.get_text(" ", strip=True).casefold() for node in table.select("thead th")]
        if set(headers) != {"model", "input", "cached input", "output"} or len(headers) != 4:
            continue
        for tr in table.select("tbody tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) != len(headers):
                continue
            values = dict(zip(headers, cells, strict=True))
            name = values["model"].get_text(" ", strip=True).removesuffix("\u2197").strip()
            model_id = _model_id(name)
            if model_id is None:
                continue
            row = {}
            for label, field in (
                ("input", "prompt_micro_per_m"),
                ("output", "completion_micro_per_m"),
                ("cached input", "prompt_cached_micro_per_m"),
            ):
                text = values[label].get_text(" ", strip=True)
                # Never interpret per-page/minute/character rates or a pair
                # of regional/discounted prices as one token rate.
                if re.fullmatch(r"\$\d+(?:\.\d+)?", text):
                    row[field] = _to_micro_per_m(text)
            if "prompt_micro_per_m" not in row or "completion_micro_per_m" not in row:
                continue
            cached_text = values["cached input"].get_text(" ", strip=True)
            if "prompt_cached_micro_per_m" not in row and cached_text not in {"", "-", "\u2014"}:
                continue
            if model_id in out and out[model_id] != row:
                raise ValueError(f"mistral: conflicting standard prices for {model_id}")
            out[model_id] = row
    return out


def parse(html: str) -> dict:
    # Keep old cards/Next.js fixtures readable; current docs tables win.
    out = _parse_embedded_json(html)
    out.update(_parse_rendered_cards(html))
    out.update(_parse_tables(html))
    return out
