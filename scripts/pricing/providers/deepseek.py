"""DeepSeek — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "deepseek"
URL = "https://api-docs.deepseek.com/quick_start/pricing"
EXPECTED_MODELS = [
    "deepseek/deepseek-v4-flash",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
