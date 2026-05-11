"""OpenAI — human-only provider config.

OpenAI's pricing pages are Cloudflare-blocked from script User-Agents
(every direct fetch returns 403 even with a real Chrome UA). We work
around this by routing through `r.jina.ai`, a free reader-proxy
service that uses a headless browser to render the page server-side
and returns clean markdown. The X-Return-Format header asks Jina for
markdown specifically (the default truncates to a summary).

We point at `platform.openai.com/docs/pricing` rather than the marketing
`/api/pricing/` because the docs page has the comprehensive table —
GPT-5.5 / 5.4 family with full Short / Long context tier breakdowns
and Standard / Batch / Flex / Priority processing tiers.

About the gpt-5.x 400s seen 2026-05-10
======================================
For a while it looked like the catalog had fictional gpt-5.4 routes
that OpenAI's API had quietly deprecated. Turns out gpt-5.x is real
and served — the 400s came from request-schema drift: the 5.x family
rejects `max_tokens` and requires `max_completion_tokens` instead.
That's a translation issue at the dispatch layer, not a catalog
issue. The catalog stays as-is; see request_transform.py for the
per-model parameter rename.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "openai"
URL = "https://r.jina.ai/https://platform.openai.com/docs/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}

# Models we expect to see on the page. Strict floor — parser must
# extract these or validation triggers self-heal. Today the page
# headlines GPT-5.5 / 5.4 family; if OpenAI replaces those with a
# 5.6 family this list needs updating.
EXPECTED_MODELS = [
    "openai/gpt-5.5",
    "openai/gpt-5.4",
    "openai/gpt-5.4-mini",
]


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
