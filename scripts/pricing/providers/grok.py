"""xAI Grok — human-only provider config.

docs.x.ai/docs/models has a server-rendered table with current
Grok pricing. Routed through r.jina.ai for clean markdown.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "grok"
URL = "https://r.jina.ai/https://docs.x.ai/docs/models"
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
