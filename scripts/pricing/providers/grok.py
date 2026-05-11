"""xAI Grok — human-only provider config.

docs.x.ai's pricing tables moved from /docs/models to /developers/pricing
in 2026-05. The new page still renders one markdown row per model with
$-priced input/output cells but with a new column order
| Model | Context | Input | Cached | Output |
instead of the old
| name | context | input | output | (cached) |
and the model name cell is now a markdown link ([grok-4.3](url)) rather
than a bare slug. The parser handles both (see parsers/grok.py).
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "grok"
URL = "https://r.jina.ai/https://docs.x.ai/developers/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}

EXPECTED_MODELS = [
    "x-ai/grok-4.3",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
