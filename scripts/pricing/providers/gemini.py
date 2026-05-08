"""Google Gemini — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "gemini"
# ai.google.dev/pricing 302-redirects to OAuth ("auto_signin=True"), so
# we'd never get HTML. cloud.google.com/vertex-ai/generative-ai/pricing
# is the public sister page that actually serves token prices in plain
# server-rendered HTML.
URL = "https://cloud.google.com/vertex-ai/generative-ai/pricing"
EXPECTED_MODELS = [
    "google/gemini-2.5-flash",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
