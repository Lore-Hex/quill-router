"""Mistral — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "mistral"
URL = "https://mistral.ai/pricing"
EXPECTED_MODELS = [
    "mistralai/mistral-small-2603",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
