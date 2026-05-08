"""Moonshot AI / Kimi — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "kimi"
URL = "https://platform.moonshot.ai/docs/pricing/chat"
EXPECTED_MODELS = [
    "moonshotai/kimi-k2.6",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
