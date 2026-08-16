"""Typed enums for fields that were previously magic strings."""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from trusted_router.catalog import Model, ModelEndpoint


class UsageType(StrEnum):
    """Whether a request bills prepaid credits or only the user's BYOK key."""

    CREDITS = "Credits"
    BYOK = "BYOK"

    @classmethod
    def for_model(cls, model: Model) -> UsageType:
        return cls.CREDITS if model.prepaid_available else cls.BYOK

    @classmethod
    def for_endpoint(cls, endpoint: ModelEndpoint) -> UsageType:
        return cls.coerce(endpoint.usage_type)

    @classmethod
    def coerce(cls, value: str | UsageType) -> UsageType:
        if isinstance(value, cls):
            return value
        normalized = str(value).strip().lower()
        if normalized == "byok":
            return cls.BYOK
        return cls.CREDITS

    def is_byok(self) -> bool:
        return self is UsageType.BYOK


class ErrorType(StrEnum):
    """Stable error type strings shared across the API surface."""

    BAD_REQUEST = "bad_request"
    UNAUTHORIZED = "unauthorized"
    FORBIDDEN = "forbidden"
    VERIFICATION_REQUIRED = "verification_required"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    ALREADY_REGISTERED = "already_registered"
    KEY_LIMIT_EXCEEDED = "key_limit_exceeded"
    KEY_WINDOW_LIMIT_EXCEEDED = "key_window_limit_exceeded"
    INSUFFICIENT_CREDITS = "insufficient_credits"
    # A FEDERATED workspace with no spendable balance on THIS plane. Distinct
    # from insufficient_credits because the customer is very likely not out of
    # money — their credits are on the home plane and have not been
    # transferred. Telling them "insufficient credits" would send them to top
    # up an account that already has a balance.
    CREDITS_NOT_ON_THIS_PLANE = "credits_not_on_this_plane"
    # A peer plane already holds the maximum unsettled spend it will carry for
    # this workspace while the home plane is unreachable. Distinct from both
    # insufficient_credits (a statement about the customer's money) and
    # credits_not_on_this_plane (a statement about where it lives): this one
    # says the customer is fine and the PLANE is at its self-imposed limit,
    # which clears as settlements are delivered. Retryable.
    DEFERRED_SETTLEMENT_CAP = "deferred_settlement_cap"
    # Federated settlement verdicts. Each is a STRUCTURED code the peer's
    # forwarder keys on — a bare 404 or an unparseable body must read as an
    # outage (deploy skew, a proxy default page, a rollback), never as one of
    # these, or a home rollback silently dead-letters the whole backlog.
    SETTLEMENT_TERMS_CONFLICT = "settlement_terms_conflict"
    WORKSPACE_UNKNOWN = "workspace_unknown"
    SETTLEMENT_CLAMPED = "settlement_clamped"
    MODEL_NOT_SUPPORTED = "model_not_supported"
    PROVIDER_AUTH_ERROR = "provider_auth_error"
    PROVIDER_ERROR = "provider_error"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    PROVIDER_NOT_SUPPORTED = "provider_not_supported"
    RATE_LIMITED = "rate_limited"
    ENDPOINT_NOT_SUPPORTED = "endpoint_not_supported"
    PRIVATE_MODELS_NOT_SUPPORTED = "private_models_not_supported"
    DEPRECATED = "deprecated"
    CONTENT_NOT_STORED = "content_not_stored"
    CONTENT_STORAGE_DISABLED = "content_storage_disabled"
    INTERNAL_ERROR = "internal_error"
    HTTP_ERROR = "http_error"
    SERVICE_UNAVAILABLE = "service_unavailable"
    INVALID_TAGS = "invalid_tags"
    INVALID_REQUEST_METADATA = "invalid_request_metadata"
