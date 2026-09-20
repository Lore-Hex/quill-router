"""Pinned provider contract identifiers shared by runtime and refresh code."""

import re

SAKANA_FUGU_MODEL_ID = "sakana-ai/fugu-ultra-v1.1"
SAKANA_NAMAZU_MODEL_ID = "sakana-ai/sakana-namazu-v1.0"
SAKANA_NAMAZU_ROUTE_HOLD_REASON = "provider-geographic-restriction"
NEXTBIT_UNSLOPNEMO_MODEL_ID = "thedrummer/unslopnemo-12b-v4.1"
NEXTBIT_UNSLOPNEMO_HOLD_REASON = "provider-alias-unavailable"
OPERATOR_HELD_PROVIDER_MODELS = frozenset(
    {
        ("sakana", SAKANA_NAMAZU_MODEL_ID),
        # A listed alias is not proof of serving availability. Require a
        # reviewed recovery before refresh can re-enable this route.
        ("nextbit", NEXTBIT_UNSLOPNEMO_MODEL_ID),
        # September 20: discovery admits these standard RedPill IDs, but the
        # enclave's reviewed Phala dispatch map does not. Keep them out of
        # routing until a reviewed adapter release, not an attestation bypass.
        ("phala", "z-ai/glm-5.3"),
        ("phala", "z-ai/glm-5.3-flash"),
    }
)
EXACT_GLOBAL_SETTLEMENT_PROVIDER_MODELS = frozenset(
    {
        ("sakana", SAKANA_FUGU_MODEL_ID),
    }
)

# Native task meters include provider-side preprocessing. Preserve the exact
# reported input count, and do not apply the generic output-token price floor.
INPUT_ONLY_PROVIDER_MODELS = frozenset(
    ("scaledown", f"scaledown/{task}")
    for task in ("compress", "summarize", "extract", "classify")
)
EXACT_GLOBAL_SETTLEMENT_PROVIDER_MODELS |= INPUT_ONLY_PROVIDER_MODELS
PASSTHROUGH_RETAIL_PROVIDER_MODELS = frozenset(
    {
        # Match the public Fugu price used by other router marketplaces while
        # preserving Sakana's exact first-party cost in its manifest.
        ("sakana", SAKANA_FUGU_MODEL_ID),
    }
)
UNSUPPORTED_GATEWAY_REGIONS_BY_PROVIDER_MODEL = {
    # Sakana's API terms exclude the EEA, UK, and Switzerland, and the provider
    # may enforce that boundary by source IP. The Europe gateway therefore
    # cannot authorize Fugu until Sakana expands its supported regions.
    ("sakana", SAKANA_FUGU_MODEL_ID): frozenset({"europe-west4"}),
    ("sakana", SAKANA_NAMAZU_MODEL_ID): frozenset({"europe-west4"}),
}

# Fugu reports additive provider-side orchestration tokens only after a call.
# They are included in exact settlement, but may exceed the caller-derived
# estimate. Regional quota leases cap settlement to their initial escrow, so
# Fugu must stay on the global typed ledger until leases support exact overruns.

# Sakana's terms exclude the EEA, UK, and Switzerland, and its edge returns an
# HTML 403 to the europe-west4 gateway. The canonical API currently includes
# every gateway region in one DNS answer, so a region-local exclusion would
# make ordinary Namazu calls fail nondeterministically. Keep the discovered
# model visible but globally unroutable until canonical steering can guarantee
# a supported egress region without bypassing Sakana's geographic policy.


def provider_model_operator_held(provider_slug: str, model_id: str) -> bool:
    return (provider_slug, model_id) in OPERATOR_HELD_PROVIDER_MODELS


def redpill_token_limit_field(upstream_id: str) -> str:
    """Redpill forwards OpenAI's modern token-cap contract without translating it."""
    model = upstream_id.removeprefix("openai/")
    generation = re.match(r"gpt-(\d+)", model)
    if (generation and int(generation[1]) >= 5) or model.startswith(("o1", "o3", "o4")):
        return "max_completion_tokens"
    return "max_tokens"


def provider_model_requires_exact_global_settlement(
    provider_slug: str,
    model_id: str,
) -> bool:
    return (provider_slug, model_id) in EXACT_GLOBAL_SETTLEMENT_PROVIDER_MODELS


def provider_model_uses_passthrough_retail_price(
    provider_slug: str,
    model_id: str,
) -> bool:
    return (provider_slug, model_id) in PASSTHROUGH_RETAIL_PROVIDER_MODELS


def provider_model_available_from_gateway_region(
    provider_slug: str,
    model_id: str,
    gateway_region: str,
) -> bool:
    unsupported = UNSUPPORTED_GATEWAY_REGIONS_BY_PROVIDER_MODEL.get(
        (provider_slug, model_id),
        frozenset(),
    )
    return gateway_region not in unsupported
