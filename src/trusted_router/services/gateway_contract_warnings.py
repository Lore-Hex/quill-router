"""Content-free visibility into authenticated pre-billing compatibility failures."""

from __future__ import annotations

import re

from trusted_router.schemas import GatewayContractRejection
from trusted_router.sentry_config import capture_gateway_contract_warning

# Public parameter categories, not a request allowlist. Unknown names might
# themselves contain customer content, so only bounded identifier paths are
# retained separately. Unknown names never create new alert fingerprints.
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
        "allow_fallbacks",
        "cache_control",
        "debug",
        "depth",
        "frequency_penalty",
        "image_config",
        "logit_bias",
        "logprobs",
        "min_p",
        "n",
        "plugins",
        "polyphemus",
        "prediction",
        "presence_penalty",
        "prompt",
        "prompt_cache_key",
        "prompt_cache_options",
        "repetition_penalty",
        "route",
        "safety_identifier",
        "seed",
        "service_tier",
        "session_id",
        "stop",
        "stop_server_tools_when",
        "top_a",
        "top_k",
        "top_logprobs",
        "trace",
        "truncation",
        "user",
        "web_search_options",
        "usage.include",
        "stream_options.include_usage",
        "provider.allow_fallbacks",
        "provider.data_collection",
        "provider.enforce_distillable_text",
        "provider.ignore",
        "provider.max_price",
        "provider.only",
        "provider.order",
        "provider.preferred_max_latency",
        "provider.preferred_min_throughput",
        "provider.quantizations",
        "provider.require_parameters",
        "provider.sort",
        "provider.sort.by",
        "provider.sort.partition",
        "provider.zdr",
        "provider.billing",
        "provider.min_privacy",
        "provider.options",
        "provider.usage",
        "provider.usage_type",
        "provider.max_price.prompt",
        "provider.max_price.completion",
        "provider.max_price.image",
        "provider.max_price.audio",
        "provider.max_price.request",
        "plugins.auto-beta-router",
        "plugins.auto-router",
        "plugins.context-compression",
        "plugins.file-parser",
        "plugins.moderation",
        "plugins.pareto-router",
        "plugins.response-healing",
        "plugins.web-fetch",
    }
)


_PARAMETER_PATH = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(\[[0-9]{1,6}\])?"
    r"(\.[A-Za-z_][A-Za-z0-9_]*(\[[0-9]{1,6}\])?){0,7}"
)


def _safe_parameter_path(value: str | None) -> str | None:
    if not value or len(value) > 128:
        return None
    if value in PARAMETER_CATEGORIES:
        return value
    if not _PARAMETER_PATH.fullmatch(value):
        return None
    if any(
        segment.startswith(("sk_", "rk_", "pk_", "key_", "ghp_", "github_pat_"))
        for segment in value.lower().split(".")
    ):
        return None
    return value


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
    context = {"request_id": rejection.request_id}
    if path := _safe_parameter_path(rejection.parameter_path):
        context["parameter_path"] = path
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
            "contexts": {"gateway_rejection": context},
        }
    )
