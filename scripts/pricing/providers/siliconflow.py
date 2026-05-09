"""SiliconFlow — human-only provider config.

SiliconFlow's pricing page is mostly image-rendered (Framer-built
marketing site). We fetch /pricing for completeness but the parser
relies on a hardcoded table for the routed models. The cross-check
vs OpenRouter and the LLM self-heal will surface drift.

OpenAI-compatible chat completions at api.siliconflow.com/v1.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "siliconflow"
URL = "https://r.jina.ai/https://www.siliconflow.com/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}

EXPECTED_MODELS: list[str] = []  # parser tolerant of upstream renames


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
