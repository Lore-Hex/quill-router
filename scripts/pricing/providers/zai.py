"""Z.AI / Zhipu — human-only provider config.

docs.z.ai/guides/llm/glm-4.6 is a per-model overview — not a price
table. The dedicated pricing page lives at /guides/overview/pricing
which has a clean text-models table. The page is partly JS-rendered
so a direct fetch returns the navigation chrome only; we route
through r.jina.ai which renders server-side and returns markdown.

All current Z.AI text models (GLM-5.1 down to GLM-4-32B-0414) are on
the page with Input / Cached Input / Output prices.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "zai"
URL = "https://r.jina.ai/https://docs.z.ai/guides/overview/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}

EXPECTED_MODELS = [
    "z-ai/glm-4.6",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
