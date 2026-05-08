"""Z.AI / Zhipu — human-only provider config."""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "zai"
# Z.AI's docs page is JS-rendered; the HTML body has no $/M-token
# literals. The parser carries a hardcoded constants table; the cross-
# check vs OR is what catches drift, and the self-heal LLM rewrites
# the table when prices change. The URL still has to fetch (so the
# pipeline runs) and ideally contains some hint of the live price the
# LLM can extract on rewrite.
URL = "https://docs.z.ai/guides/llm/glm-4.6"
EXPECTED_MODELS = [
    "z-ai/glm-4.6",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(slug=SLUG, url=URL, expected_models=EXPECTED_MODELS)
