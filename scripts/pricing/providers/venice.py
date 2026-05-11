"""Venice — human-only provider config.

docs.venice.ai/overview/pricing has a clean repeating-card layout
with each model's input/output rates. Routed through r.jina.ai for
clean markdown.

Venice uses its own model-id namespace (`zai-org-glm-4.6`, not
`z-ai/glm-4.6` or `glm-4.6`). Until Nov 2026 the venice API
silently aliased OR-style ids back to its native ids; that alias
went away and now /v1/chat/completions returns 404 if the request
body's `model` isn't the exact native id. UPSTREAM_ID_MAP is the
inverse of parsers/venice.py::_NAME_TO_OR_ID and is consumed by
refresh.py at merge time to override the snapshot endpoint's
`model_id` (which the enclave sends verbatim to Venice). Living in
the human-only provider config means the LLM self-heal cannot touch
this mapping — it's authoritative routing config, not parser logic.
"""
from __future__ import annotations

from scripts.pricing.base import ProviderPricingResult, fetch_provider

SLUG = "venice"
URL = "https://r.jina.ai/https://docs.venice.ai/overview/pricing"
JINA_HEADERS = {"X-Return-Format": "markdown"}

EXPECTED_MODELS = [
    "z-ai/glm-4.6",
]

# OR-canonical id → Venice native id (what the enclave puts in
# request.model). Keep this in sync with parsers/venice.py's
# _NAME_TO_OR_ID (the inverse). The parser maps native→OR for
# indexing pricing rows; this map goes OR→native for the upstream
# request body. If the parser learns a new model, mirror it here.
UPSTREAM_ID_MAP = {
    "z-ai/glm-5.1": "zai-org-glm-5-1",
    "z-ai/glm-5": "zai-org-glm-5",
    "z-ai/glm-5-turbo": "z-ai-glm-5-turbo",
    "z-ai/glm-5v-turbo": "z-ai-glm-5v-turbo",
    "z-ai/glm-4.7-flash": "zai-org-glm-4.7-flash",
    "z-ai/glm-4.7": "zai-org-glm-4.7",
    "z-ai/glm-4.6": "zai-org-glm-4.6",
    "qwen/qwen3.6-27b": "qwen3-6-27b",
    "qwen/qwen3.5-9b": "qwen3-5-9b",
    "qwen/qwen3.5-397b-a17b": "qwen3-5-397b-a17b",
    "qwen/qwen3-235b-a22b-thinking-2507": "qwen3-235b-a22b-thinking-2507",
    "qwen/qwen3-235b-a22b-instruct-2507": "qwen3-235b-a22b-instruct-2507",
    "qwen/qwen3-next-80b": "qwen3-next-80b",
    "qwen/qwen3-vl-235b-a22b": "qwen3-vl-235b-a22b",
    "qwen/qwen3-coder-480b-a35b-instruct-turbo": "qwen3-coder-480b-a35b-instruct-turbo",
}


def fetch() -> ProviderPricingResult:
    return fetch_provider(
        slug=SLUG,
        url=URL,
        expected_models=EXPECTED_MODELS,
        extra_headers=JINA_HEADERS,
    )
