"""Content-free visibility into authenticated pre-billing compatibility failures."""

from __future__ import annotations

from trusted_router.schemas import GatewayContractRejection
from trusted_router.sentry_config import capture_gateway_contract_warning

# Public parameter categories, not a request allowlist. Unknown names might
# themselves contain customer content, so never export them or fingerprint them.
PARAMETER_CATEGORIES = frozenset(
    {
        "store",
        "model",
        "models",
        "messages",
        "input",
        "instructions",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "stream",
        "stream_options",
        "text",
        "response_format",
        "temperature",
        "top_p",
        "reasoning",
        "reasoning_effort",
        "provider",
        "metadata",
        "tags",
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
        "max_tool_calls",
        "previous_response_id",
        "conversation",
        "background",
        "include",
        "modalities",
        "prompt_cache_retention",
        "usage",
        "other",
    }
)


def report_gateway_contract_rejection(
    rejection: GatewayContractRejection | None,
    *,
    route: str | None,
    workspace_id: str,
    credential_id: str,
) -> None:
    if rejection is None or route not in {"/v1/chat/completions", "/v1/responses"}:
        return
    parameter = rejection.parameter if rejection.parameter in PARAMETER_CATEGORIES else "other"
    capture_gateway_contract_warning(
        {
            "level": "warning",
            "message": "Authenticated gateway request rejected by the API contract",
            "fingerprint": ["gateway-contract-rejection", route, str(rejection.status), parameter],
            "tags": {
                "component": "gateway-contract",
                "route": route,
                "http_status": str(rejection.status),
                "parameter": parameter,
                "workspace_id": workspace_id,
                "credential_id": credential_id,
            },
            "contexts": {"gateway_rejection": {"request_id": rejection.request_id}},
        }
    )
