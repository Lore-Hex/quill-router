"""Phala (RedPill) — human-only provider config.

Phala runs all inference inside Intel TDX + NVIDIA Confidential
Compute enclaves. Verified attestation, end-to-end encrypted prompts.
The marketing landing page red-pill.ai lists every served model
with inline pricing, which is what we parse.

OpenAI-compatible chat completions at api.red-pill.ai/v1.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "phala"
URL = "https://r.jina.ai/https://red-pill.ai/"
JINA_HEADERS = {"X-Return-Format": "markdown"}

EXPECTED_MODELS: list[str] = []  # let the parser report what it finds; no strict floor


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
