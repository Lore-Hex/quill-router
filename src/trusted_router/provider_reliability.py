"""Provider failure attribution and synthetic deadline policy.

The public leaderboard, provider portal, and route-health automation all need
the same answer to two questions:

* Did this request complete within the model's advertised first-token budget?
* If it did not, who owns the failure?

Keep that policy here so reporting cannot quietly diverge between surfaces.
The classifier is deliberately conservative: ambiguous failures remain
``unknown`` instead of being charged to a provider.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trusted_router.synthetic.components import is_router_origin_error


class FailureOwner(StrEnum):
    NONE = "none"
    PROVIDER = "provider"
    TRUSTEDROUTER = "trustedrouter"
    CONFIGURATION = "configuration"
    CUSTOMER = "customer"
    UNKNOWN = "unknown"


class FailureClass(StrEnum):
    SUCCESS = "success"
    PROVIDER_CAPACITY = "provider_capacity"
    PROVIDER_INTERNAL = "provider_internal"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_STREAM = "provider_stream"
    TRUSTEDROUTER_CAPACITY = "trustedrouter_capacity"
    ROUTER_FAULT = "router_fault"
    CUSTOMER_QUOTA = "customer_quota"
    PROVIDER_AUTH_CONFIG = "provider_auth_config"
    UNSUPPORTED_ROUTE = "unsupported_route"
    PROBE_CONFIG = "probe_config_error"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureAttribution:
    owner: FailureOwner
    failure_class: FailureClass
    counts_toward_provider_availability: bool
    capacity_rejected: bool = False


_ACCOUNT_QUOTA_MARKERS = (
    "account quota",
    "billing quota",
    "credit balance",
    "credits exhausted",
    "insufficient credit",
    "insufficient funds",
    "monthly spend",
    "payment required",
    "quota exceeded for quota metric",
)
_CUSTOMER_QUOTA_TYPES = frozenset(
    {
        "credit_limit_exceeded",
        "insufficient_credits",
        "key_limit_exceeded",
        "rate_limit_exceeded",
        "workspace_limit_exceeded",
    }
)
_CONFIG_TYPES = frozenset(
    {
        "bad_request",
        "invalid_request",
        "invalid_request_error",
        "model_not_available",
        "model_not_found",
        "not_found",
        "not_supported",
        "probe_config_error",
        "provider_auth_config",
        "unsupported",
        "unsupported_model",
        "unsupported_provider",
        "unsupported_route",
    }
)
_TIMEOUT_MARKERS = ("timeout", "timed_out", "deadline", "readtimeout", "connecttimeout")
_STREAM_MARKERS = ("empty_stream", "stream_error", "stream_interrupted", "incomplete_stream")


def classify_provider_failure(
    *,
    status: str,
    error_type: str | None,
    error_status: int | None,
    error_message: str | None = None,
) -> FailureAttribution:
    """Classify one metadata-only provider observation.

    An upstream account quota is owned by TrustedRouter because provisioning
    enough provider capacity is our responsibility. A generic 429/503 remains
    provider capacity unless the provider explicitly identifies an account
    quota. Router-origin errors never lower a provider's availability.
    """

    if status == "success":
        return FailureAttribution(FailureOwner.NONE, FailureClass.SUCCESS, True)

    kind = (error_type or "").strip().casefold()
    message = (error_message or "").strip().casefold()

    if kind in _CUSTOMER_QUOTA_TYPES:
        return FailureAttribution(
            FailureOwner.CUSTOMER,
            FailureClass.CUSTOMER_QUOTA,
            False,
        )
    if is_router_origin_error(error_type):
        return FailureAttribution(
            FailureOwner.TRUSTEDROUTER,
            FailureClass.ROUTER_FAULT,
            False,
        )
    if status == "unsupported" or kind in _CONFIG_TYPES or error_status in {400, 404, 422}:
        failure_class = (
            FailureClass.PROVIDER_AUTH_CONFIG
            if error_status in {401, 403} or kind == "provider_auth_config"
            else FailureClass.PROBE_CONFIG
            if kind == "probe_config_error"
            else FailureClass.UNSUPPORTED_ROUTE
        )
        return FailureAttribution(FailureOwner.CONFIGURATION, failure_class, False)
    if error_status in {401, 403}:
        return FailureAttribution(
            FailureOwner.CONFIGURATION,
            FailureClass.PROVIDER_AUTH_CONFIG,
            False,
        )
    if error_status == 402 or any(marker in message for marker in _ACCOUNT_QUOTA_MARKERS):
        return FailureAttribution(
            FailureOwner.TRUSTEDROUTER,
            FailureClass.TRUSTEDROUTER_CAPACITY,
            False,
            capacity_rejected=True,
        )
    if error_status in {429, 503, 529} or "overload" in kind or "capacity" in kind:
        return FailureAttribution(
            FailureOwner.PROVIDER,
            FailureClass.PROVIDER_CAPACITY,
            True,
            capacity_rejected=True,
        )
    if any(marker in kind for marker in _TIMEOUT_MARKERS):
        return FailureAttribution(
            FailureOwner.PROVIDER,
            FailureClass.PROVIDER_TIMEOUT,
            True,
        )
    if any(marker in kind for marker in _STREAM_MARKERS):
        return FailureAttribution(
            FailureOwner.PROVIDER,
            FailureClass.PROVIDER_STREAM,
            True,
        )
    if error_status is not None and 500 <= error_status <= 599:
        return FailureAttribution(
            FailureOwner.PROVIDER,
            FailureClass.PROVIDER_INTERNAL,
            True,
        )
    return FailureAttribution(FailureOwner.UNKNOWN, FailureClass.UNKNOWN, False)


@dataclass(frozen=True)
class ModelDeadlines:
    first_token_seconds: float
    completion_seconds: float


_SLOW_REASONING_MARKERS = (
    "reasoning",
    "thinking",
    "deep-research",
    "deepseek-r",
    "gpt-5.5",
    "gpt-5.6",
    "glm-5",
    "opus",
    "/o1",
    "/o3",
    "/o4",
)
_FAST_MARKERS = ("flash-lite", "haiku", "cerebras/", "groq/", "fast")


def model_deadlines(
    model_id: str,
    *,
    provider: str | None = None,
    default_first_token_seconds: float = 20.0,
) -> ModelDeadlines:
    """Return a bounded, model-aware synthetic deadline.

    Provider Contract v2 lets providers publish exact budgets. Until a catalog
    row carries one, this conservative policy prevents known reasoning models
    from being declared down under the same budget as low-latency models.
    """

    if default_first_token_seconds <= 0:
        raise ValueError("default_first_token_seconds must be positive")
    endpoint_first_token: float | None = None
    endpoint_completion: float | None = None
    if provider:
        # Lazy import avoids a catalog construction cycle at module import.
        from trusted_router.catalog import MODEL_ENDPOINTS

        endpoint = next(
            (
                candidate
                for candidate in MODEL_ENDPOINTS.values()
                if candidate.model_id == model_id
                and candidate.provider == provider
                and candidate.usage_type == "Credits"
                and candidate.catalog_is_current()
            ),
            None,
        )
        if endpoint is not None:
            endpoint_first_token = endpoint.first_token_timeout_seconds
            endpoint_completion = endpoint.completion_timeout_seconds
    if endpoint_first_token is not None:
        return ModelDeadlines(
            first_token_seconds=min(max(endpoint_first_token, 5.0), 300.0),
            completion_seconds=min(
                max(endpoint_completion or endpoint_first_token * 4, 30.0),
                900.0,
            ),
        )

    normalized = model_id.casefold()
    first_token = float(default_first_token_seconds)
    if any(marker in normalized for marker in _SLOW_REASONING_MARKERS):
        first_token = max(first_token, 45.0)
    elif any(marker in normalized for marker in _FAST_MARKERS):
        first_token = min(first_token, 15.0)
    first_token = min(max(first_token, 5.0), 90.0)
    return ModelDeadlines(
        first_token_seconds=first_token,
        completion_seconds=min(max(first_token * 4, 30.0), 300.0),
    )
