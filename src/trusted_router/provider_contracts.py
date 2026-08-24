"""Pinned provider contract identifiers shared by runtime and refresh code."""

SAKANA_FUGU_MODEL_ID = "sakana-ai/fugu-ultra-v1.1"
SAKANA_FUGU_ROUTE_HOLD_REASON = "unbounded-provider-orchestration-cost"
SAKANA_NAMAZU_MODEL_ID = "sakana-ai/sakana-namazu-v1.0"
SAKANA_NAMAZU_ROUTE_HOLD_REASON = "provider-geographic-restriction"
OPERATOR_HELD_PROVIDER_MODELS = frozenset(
    {
        ("sakana", SAKANA_FUGU_MODEL_ID),
        ("sakana", SAKANA_NAMAZU_MODEL_ID),
    }
)

# Fugu cannot be enabled by changing its manifest alone. Its provider-side
# orchestration is not bounded by the caller's max output tokens, so authorize
# cannot reserve a trustworthy ceiling. Enabling it requires a bounded provider
# contract plus a durable authorization-time lower bound for its private tier
# selector.

# Sakana's terms exclude the EEA, UK, and Switzerland, and its edge returns an
# HTML 403 to the europe-west4 gateway. The canonical API currently includes
# every gateway region in one DNS answer, so a region-local exclusion would
# make ordinary Namazu calls fail nondeterministically. Keep the discovered
# model visible but globally unroutable until canonical steering can guarantee
# a supported egress region without bypassing Sakana's geographic policy.


def provider_model_operator_held(provider_slug: str, model_id: str) -> bool:
    return (provider_slug, model_id) in OPERATOR_HELD_PROVIDER_MODELS
