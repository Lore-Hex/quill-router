"""Shared public copy for the dated gateway processing measurement."""

GATEWAY_PROCESSING_MEDIAN_MS, MEASUREMENT_DATE, PROVIDER_FIRST_BYTE_SECONDS = (
    3, "November 24, 2026", 0.7,
)
GATEWAY_HOP_CLAIM = (
    "Any hosted gateway adds one network hop between the client and the provider; "
    "OpenRouter is the same extra hop."
)


def gateway_processing_claim() -> str:
    return (
        f"Gateway processing takes a median of {GATEWAY_PROCESSING_MEDIAN_MS} milliseconds "
        "per request, counting only time inside the gateway on the request itself and "
        "excluding account authorization and billing settlement calls and network time "
        "between the client, gateway, and provider "
        f"(measured {MEASUREMENT_DATE} on successful chat and responses requests "
        "across all four gateway regions). "
        "In the same sample, the provider's median time to first byte was about "
        f"{PROVIDER_FIRST_BYTE_SECONDS} seconds, so gateway processing is well under 1% "
        "of the wait for the first token."
    )
