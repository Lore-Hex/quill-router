"""Google Gemini — human-only provider config.

ai.google.dev/pricing 302-redirects to OAuth, so a direct fetch never
returns HTML. We route through `r.jina.ai` against the docs path
(`/gemini-api/docs/pricing`) which Jina renders server-side and
returns as markdown. This bypass works because Jina hits the page
from its own infrastructure (with whatever cookies/state it carries)
and gives us back the rendered content.

The Gemini docs pricing page has explicit context-tier breakdowns
(e.g. "$1.25, prompts <= 200k tokens / $2.50, prompts > 200k tokens"
for Gemini 2.5 Pro) which the parser converts into PriceTier objects
for tier-aware billing.

Vertex AI pricing (cloud.google.com/vertex-ai/...) is a different
surface that TR doesn't route to and is intentionally ignored.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "gemini"
URL = "https://r.jina.ai/https://ai.google.dev/gemini-api/docs/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}

EXPECTED_MODELS = [
    "google/gemini-2.5-pro",
    "google/gemini-2.5-flash",
    "google/gemini-2.5-flash-lite",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
