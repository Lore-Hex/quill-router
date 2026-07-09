"""xAI Grok — human-only provider config.

docs.x.ai's pricing tables moved from /docs/models to /developers/pricing
in 2026-05. The current page renders one row per model with $-priced
input/cache/output cells. The model name cell is often a markdown link
([grok-4.5](url)) rather than a bare slug. The parser handles both
(see parsers/grok.py).
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "grok"
URL = "https://r.jina.ai/https://docs.x.ai/developers/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}

EXPECTED_MODELS = [
    "x-ai/grok-4.5",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
