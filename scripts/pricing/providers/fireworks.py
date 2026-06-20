"""Fireworks AI — human-only provider config.

Fireworks publishes a first-party serverless pricing table for its headline
models. We fetch that docs page and parse the standard serving-path prices.
The live model list is handled separately by provider_models/fireworks.json.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "fireworks"
URL = "https://r.jina.ai/https://docs.fireworks.ai/serverless/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}

EXPECTED_MODELS = [
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k2.5",
    "deepseek/deepseek-v4-pro",
    "z-ai/glm-5.2",
    "z-ai/glm-5.1",
    "openai/gpt-oss-120b",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
