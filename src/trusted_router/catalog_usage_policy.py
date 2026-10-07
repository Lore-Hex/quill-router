"""Public disclosure for reviewed provider-specific usage estimation."""

ABLITERATE_ESTIMATED_USAGE_NOTICE = (
    "When Abliterate omits usage, successful prepaid calls are billed using up to "
    "twice the locally estimated input and output tokens, bounded by the request's "
    "authorized budget. Estimated output is also bounded by the requested output "
    "limit. Responses mark usage_estimated=true. Provider-reported usage takes "
    "precedence. These are estimates, not verified upstream token counts."
)


def provider_usage_estimation_policy(provider: str) -> dict[str, object] | None:
    if provider != "abliterate":
        return None
    return {
        "policy": "abliterate_conservative_v1",
        "maximum_estimate_multiplier": 2,
        "provider_usage_preferred": True,
        "authorized_budget_bounded": True,
        "prepaid_only": True,
        "description": ABLITERATE_ESTIMATED_USAGE_NOTICE,
    }
