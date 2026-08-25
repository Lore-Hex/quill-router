"""Pinned provider contract identifiers shared by runtime and refresh code."""

SAKANA_FUGU_MODEL_ID = "sakana-ai/fugu-ultra-v1.1"
SAKANA_NAMAZU_MODEL_ID = "sakana-ai/sakana-namazu-v1.0"
SAKANA_NAMAZU_ROUTE_HOLD_REASON = "provider-geographic-restriction"
OPERATOR_HELD_PROVIDER_MODELS = frozenset(
    {
        ("sakana", SAKANA_NAMAZU_MODEL_ID),
    }
)
EXACT_GLOBAL_SETTLEMENT_PROVIDER_MODELS = frozenset(
    {
        ("sakana", SAKANA_FUGU_MODEL_ID),
    }
)
PASSTHROUGH_RETAIL_PROVIDER_MODELS = frozenset(
    {
        # Match the public Fugu price used by other router marketplaces while
        # preserving Sakana's exact first-party cost in its manifest.
        ("sakana", SAKANA_FUGU_MODEL_ID),
    }
)

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
