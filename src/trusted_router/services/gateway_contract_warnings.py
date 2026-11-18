"""Content-free visibility into authenticated pre-billing compatibility failures.

A contract rejection is a customer request we deliberately refused with a 4xx
or 501 (``store=true``, an unsupported ``tools[].type`` ...). That is an
expected client error, not a server fault, so it goes to the structured
operational log (Axiom), never to Sentry: every distinct route/status/parameter
used to open its own Sentry issue and buried real faults. The log line keeps
``parameter_path``, ``http_status`` and the verified workspace so product can
still count which unsupported parameters are in demand.
"""

from __future__ import annotations

import logging
import re

from trusted_router.schemas import (
    GATEWAY_REJECTION_PARAMETER_PATH_MAX_LENGTH,
    GatewayContractRejection,
)
from trusted_router.services.contract_value_preview import safe_value_preview
from trusted_router.storage_operational_analytics import analytics_surrogate

logger = logging.getLogger(__name__)

# Public parameter categories, not a request allowlist. Unknown names might
# themselves contain customer content, so only bounded identifier paths are
# retained separately. Unknown names never create new alert fingerprints.
PARAMETER_CATEGORIES = frozenset(
    {
        "deidentify",
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
        "output_config",
        "output_config.effort",
        "include_reasoning",
        "thinking",
        "enable_thinking",
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
        "file",
        "audio",
        "video",
        "content",
        "input_image",
        "input_audio",
        "input_file",
        "allow_fallbacks",
        "cache_control",
        "chat_template_kwargs",
        "client_metadata",
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
        "repeat_penalty",
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
_PATH_INDEX = re.compile(r"\[[0-9]{1,6}\]")


def _rejected_field(path: str) -> str:
    """The path without its item indices, so input[3].content and
    input[148].content count as one field in Sentry's tag breakdown."""
    return _PATH_INDEX.sub("[]", path)


def _safe_parameter_path(value: str | None) -> str | None:
    if not value or len(value) > GATEWAY_REJECTION_PARAMETER_PATH_MAX_LENGTH:
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
    """Log one content-free line per rejection. Never raises, never reaches Sentry."""
    if rejection is None or route not in {"/v1/chat/completions", "/v1/responses"}:
        return
    try:
        parameter = rejection.parameter if rejection.parameter in PARAMETER_CATEGORIES else "other"
        fields: dict[str, str | bool] = {
            "contract_route": route,
            "http_status": str(rejection.status),
            "parameter": parameter,
            "request_id": rejection.request_id,
            "workspace_id": workspace_id,
            # The analytics surrogate (sha256, resolvable through
            # tr.workspace_directory) lets a query count distinct workspaces:
            # one workspace sending a field no contract defines is that
            # client's mistake; many refused the same way is our gap.
            "workspace_surrogate": analytics_surrogate("workspace", workspace_id),
            "credential_id": credential_id,
        }
        if path := _safe_parameter_path(rejection.parameter_path):
            fields["parameter_path"] = path
            # Indices removed, so input[3].content and input[148].content
            # aggregate as one field.
            fields["rejected_field"] = _rejected_field(path)
            preview, trimmed = safe_value_preview(path, rejection.value_preview)
            if preview is not None:
                fields["value_preview"] = preview
                fields["value_truncated"] = rejection.value_truncated or trimmed
        logger.info("gateway.contract_rejection", extra=fields)
    except Exception:  # noqa: BLE001 - telemetry cannot change request validation
        logger.warning("gateway contract rejection log failed")
