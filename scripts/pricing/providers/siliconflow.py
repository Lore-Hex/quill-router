"""SiliconFlow — human-only provider config.

SiliconFlow's pricing page is a Framer site whose server-rendered model cards
contain the authoritative prices. The parser reads those cards directly.

OpenAI-compatible chat completions at api.siliconflow.com/v1.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "siliconflow"
URL = "https://www.siliconflow.com/pricing"

EXPECTED_MODELS: list[str] = []  # parser tolerant of upstream renames


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
    )
