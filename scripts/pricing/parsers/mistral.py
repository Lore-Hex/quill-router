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
        return int(round(float(match.group(1)) * 1_000_000))
    except (TypeError, ValueError):
        return None


def parse(html: str) -> dict:
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
