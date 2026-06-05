"""Cerebras — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "cerebras"
URL = "https://www.cerebras.ai/pricing"
EXPECTED_MODELS = [
    "openai/gpt-oss-120b",
    "z-ai/glm-4.7",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
