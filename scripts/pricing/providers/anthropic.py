"""Anthropic — human-only provider config.

Hardcoded URL and the set of model IDs we expect to find on Anthropic's
pricing page (drift detector). The LLM never reads or writes this file.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "anthropic"
URL = "https://www.anthropic.com/pricing"
# Model IDs we expect Anthropic to publish on its pricing page. Listed
# in OpenRouter canonical form (`anthropic/<slug>`) — parsers translate
# whatever the page says into these IDs.
EXPECTED_MODELS = [
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-haiku-4.5",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
