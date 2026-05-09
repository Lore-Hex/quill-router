"""Novita AI — human-only provider config.

novita.ai/pricing has a server-rendered list of all served models
with input/output rates per row. Routed through r.jina.ai for clean
markdown.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "novita"
URL = "https://r.jina.ai/https://novita.ai/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}

EXPECTED_MODELS = [
    "deepseek/deepseek-v4-flash",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
