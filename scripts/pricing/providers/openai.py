"""OpenAI — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "openai"
URL = "https://openai.com/api/pricing/"
EXPECTED_MODELS = [
    "openai/gpt-4o-mini",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
