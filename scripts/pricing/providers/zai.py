"""Z.AI / Zhipu — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "zai"
URL = "https://docs.z.ai/guides/llm/glm-4.6"
EXPECTED_MODELS = [
    "z-ai/glm-4.6",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
