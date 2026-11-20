"""Closed-weight models route only through their maker and authorized clouds.

A closed-weight model has exactly one source of truth: its maker's API, plus
the clouds the maker licenses to sell it. Third-party catalogs list a maker's
public ids the day they launch (GMI listed google/gemini-4-carbon, atlas-cloud
gemini-3.8-flash, novita GPT-6.2 variants), but a reseller that wraps another
API is not an authorized source. Listing an id is not an admission.

The rule applies to every usage type: Credits routes would spend our money with
an unauthorized intermediary, and BYOK routes would present that intermediary
as a supported way to reach the model. It is evaluated at final catalog
admission, so neither the hourly refresh nor a stale snapshot can restore a
route.

Families are whole maker namespaces and fail closed: a new closed model from
one of these makers is restricted without a code change. Open-weight releases
in the same namespace are exempt; anyone may host published weights, and
`catalog.model_open_weights` must never call one of these restricted.

A maker whose namespace mixes closed and open releases is restricted by model
prefix instead. MiniMax publishes its M-series LLM weights, so only its closed
Hailuo video models are restricted; the rest of minimax/* stays open to any
host.
"""

from __future__ import annotations

# Maker namespace -> providers authorized to serve its closed-weight models.
# Provider slugs are catalog provider ids: "grok" is xAI's own API, "azure" is
# Azure Foundry (Models sold by Azure, docs/providers/azure-openai-zdr.md), and
# Claude on Vertex and Bedrock is Anthropic's own cloud distribution
# (product decision 2026-07-18, docs/providers/bedrock-runtime.md).
# "byteplus" is ByteDance's own ModelArk API for Seed/Doubao text and Seedance
# video; "runway" is Runway's own API.
CLOSED_WEIGHT_FAMILY_PROVIDERS: dict[str, frozenset[str]] = {
    "anthropic": frozenset({"anthropic", "google-vertex", "amazon-bedrock"}),
    "bytedance": frozenset({"byteplus"}),
    "bytedance-seed": frozenset({"byteplus"}),
    "google": frozenset({"google-ai-studio", "google-vertex"}),
    "openai": frozenset({"openai", "azure"}),
    "runway": frozenset({"runway"}),
    "x-ai": frozenset({"grok", "azure"}),
}

# Closed model lines inside an otherwise open namespace, keyed by id prefix.
# MiniMax Hailuo video (Hailuo 3 is also published as H3, and the earlier
# Hailuo API ids were video-01*) routes only through MiniMax's own API.
CLOSED_WEIGHT_PREFIX_PROVIDERS: dict[str, frozenset[str]] = {
    "minimax/hailuo": frozenset({"minimax"}),
    "minimax/h3": frozenset({"minimax"}),
    "minimax/video-01": frozenset({"minimax"}),
}

# Published weights inside a closed maker's namespace. Gemma (and its
# derivatives such as EmbeddingGemma), DePlot, gpt-oss and Whisper large are
# independently hostable, as are ByteDance Seed's released checkpoints:
# Seed-OSS, Seed-Coder and UI-TARS (Apache-2.0 / MIT on Hugging Face).
_OPEN_WEIGHT_PREFIXES: tuple[str, ...] = (
    "bytedance/seed-oss",
    "bytedance/seed-coder",
    "bytedance/ui-tars",
    "bytedance-seed/seed-oss",
    "bytedance-seed/seed-coder",
    "bytedance-seed/ui-tars",
    "google/gemma",
    "google/deplot",
    "openai/gpt-oss",
    "openai/whisper-large",
)
_OPEN_WEIGHT_CONTAINS: tuple[str, ...] = ("gemma", "gpt-oss")
# xAI's released checkpoints match exactly: every other x-ai/grok* id is closed,
# including API models such as grok-2-1212 and malformed reseller spellings
# such as GMI's x-ai/grok4.6 and x-ai/grok4.7 (no hyphen).
_OPEN_WEIGHT_EXACT: frozenset[str] = frozenset({"x-ai/grok-1", "x-ai/grok-2"})

# Narrower per-model admissions inside a family. Each model here was verified
# end to end on these providers only (docs/providers/2026-11-10-gemini-carbon.md);
# adding a provider is a reviewed admission, never something discovery does.
FIRST_PARTY_ONLY_MODEL_PROVIDERS: dict[str, frozenset[str]] = {
    "google/gemini-4-carbon": frozenset({"google-ai-studio", "google-vertex"}),
}


def closed_weight_family(model_id: str) -> str | None:
    """Return the restricted family (a maker namespace, or a closed model-id
    prefix inside an open namespace) when `model_id` is a closed-weight model."""
    lowered = model_id.lower()
    for prefix in CLOSED_WEIGHT_PREFIX_PROVIDERS:
        if lowered.startswith(prefix):
            return prefix
    namespace, separator, _ = lowered.partition("/")
    if not separator or namespace not in CLOSED_WEIGHT_FAMILY_PROVIDERS:
        return None
    if lowered in _OPEN_WEIGHT_EXACT or lowered.startswith(_OPEN_WEIGHT_PREFIXES):
        return None
    if any(marker in lowered for marker in _OPEN_WEIGHT_CONTAINS):
        return None
    return namespace


def authorized_closed_weight_providers(model_id: str) -> frozenset[str] | None:
    """Providers allowed to carry `model_id`, or None when unrestricted."""
    override = FIRST_PARTY_ONLY_MODEL_PROVIDERS.get(model_id)
    if override is not None:
        return override
    family = closed_weight_family(model_id)
    if family is None:
        return None
    return CLOSED_WEIGHT_PREFIX_PROVIDERS.get(family) or CLOSED_WEIGHT_FAMILY_PROVIDERS[family]


def closed_weight_route_disallowed(provider_slug: str, model_id: str) -> bool:
    """Whether a provider route for a closed-weight model is unauthorized."""
    allowed = authorized_closed_weight_providers(model_id)
    return allowed is not None and provider_slug not in allowed
