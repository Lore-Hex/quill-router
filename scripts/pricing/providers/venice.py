"""Venice — human-only provider config.

docs.venice.ai/overview/pricing has a clean repeating-card layout
with each model's input/output rates. Routed through r.jina.ai for
clean markdown.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "venice"
URL = "https://r.jina.ai/https://docs.venice.ai/overview/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}

EXPECTED_MODELS = [
    "z-ai/glm-4.6",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
