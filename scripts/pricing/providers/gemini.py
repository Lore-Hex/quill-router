"""Google Gemini — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "gemini"
URL = "https://ai.google.dev/pricing"
EXPECTED_MODELS = [
    "google/gemini-2.5-flash",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
