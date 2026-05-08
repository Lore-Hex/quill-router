"""Moonshot AI / Kimi — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "kimi"
# Moonshot's pricing page is fully JS-rendered — the HTML returned has
# no $/M-token literals. We point at the platform page anyway so the
# fetch succeeds (200 OK, ~35KB nav HTML), but the parser carries a
# hardcoded constants table for the models we route. Cross-check vs OR
# catches drift.
URL = "https://platform.moonshot.ai/pricing"
EXPECTED_MODELS = [
    "moonshotai/kimi-k2.6",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
