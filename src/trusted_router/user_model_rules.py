from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from trusted_router.catalog import MODELS, PROVIDERS, Model, ModelEndpoint
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.services.safe_egress import aassert_public_url
from trusted_router.storage_custom_models import custom_model_id_from_slug, custom_model_slug
from trusted_router.storage_models import UserProvidedModel
from trusted_router.types import ErrorType

GATEWAY_RESERVATION_TTL_SECONDS = 2 * 60 * 60
_OWNER_FAULT_ERROR_TYPES = frozenset(
    {
        "timeout",
        str(ErrorType.USER_MODEL_TIMEOUT),
        "connection_error",
        str(ErrorType.PROVIDER_ERROR),
        "malformed_response",
    }
)

_STATIC_RESERVED_NAMES = {
    "openai",
    "anthropic",
    "google",
    "gpt",
    "claude",
    "gemini",
    "official",
    "trustedrouter",
    "admin",
    "support",
    "human",
    "verified",
}
_RESERVED_NAMES = frozenset(
    _STATIC_RESERVED_NAMES
    | set(PROVIDERS)
    | {model_id.partition("/")[0] for model_id in MODELS if "/" in model_id}
)


@dataclass(frozen=True)
class DispatchBudget:
    connect: int
    first_byte: int
    idle: int
    total: int


_DISPATCH_BUDGETS = {
    "machine": DispatchBudget(connect=10, first_byte=30, idle=60, total=300),
    "agent": DispatchBudget(connect=10, first_byte=60, idle=60, total=600),
    "human": DispatchBudget(connect=10, first_byte=300, idle=120, total=900),
}


def user_model_gateway_pair(
    *,
    model_id: str,
    name: str,
    revision: int,
    prompt_price_microdollars_per_m: int,
    completion_price_microdollars_per_m: int,
    owner_user_id: str,
    upstream_model_id: str | None = None,
) -> tuple[Model, ModelEndpoint]:
    """Build the Credits-only catalog sentinel from explicit frozen values."""
    if not model_id or revision < 1 or not owner_user_id:
        raise ValueError("invalid frozen user-model attribution")
    model = Model(
        id=model_id,
        name=name or model_id,
        provider="trustedrouter",
        context_length=1_000_000,
        upstream_id=upstream_model_id or model_id,
        prepaid_available=True,
        byok_available=False,
        prompt_price_microdollars_per_million_tokens=prompt_price_microdollars_per_m,
        completion_price_microdollars_per_million_tokens=(
            completion_price_microdollars_per_m
        ),
    )
    endpoint = ModelEndpoint(
        id=f"{model_id}@trustedrouter/credits",
        model_id=model_id,
        provider="trustedrouter",
        usage_type="Credits",
        upstream_id=upstream_model_id or model_id,
        prompt_price_microdollars_per_million_tokens=prompt_price_microdollars_per_m,
        completion_price_microdollars_per_million_tokens=(
            completion_price_microdollars_per_m
        ),
    )
    return model, endpoint


def is_owner_fault(error_status: int | None, error_type: str | None) -> bool:
    """Match the owner-health rule used by local and attested dispatch.

    An upstream 5xx or an explicit transport/timeout/malformed-response token
    says the owner endpoint failed to serve. A 4xx, cancellation, or missing
    error evidence says nothing about endpoint health and must not add a strike.
    """
    return (error_status is not None and error_status >= 500) or (
        str(error_type or "").strip().lower() in _OWNER_FAULT_ERROR_TYPES
    )


def reserved_user_model_names() -> frozenset[str]:
    return _RESERVED_NAMES


def validate_user_model_slug(slug: str) -> str:
    try:
        model_id = custom_model_id_from_slug(slug)
    except ValueError as exc:
        # An owner-typed slug that fails the grammar is their input error,
        # not a server fault.
        raise api_error(
            400,
            "Slug may contain only lowercase letters, digits, and hyphens",
            ErrorType.BAD_REQUEST,
        ) from exc
    normalized = custom_model_slug(model_id)
    if normalized in _RESERVED_NAMES:
        raise api_error(400, "This model slug is reserved", ErrorType.BAD_REQUEST)
    return normalized


def validate_user_model_display_name(display_name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", display_name.strip().lower()).strip("-")
    compact = normalized.replace("-", "")
    if not normalized or normalized in _RESERVED_NAMES or compact in _RESERVED_NAMES:
        raise api_error(400, "This display name is reserved", ErrorType.BAD_REQUEST)
    return display_name.strip()


async def validate_endpoint_url(url: str, settings: Settings) -> str:
    """Normalize and SSRF-check an owner endpoint URL.

    Async on purpose: the DNS lookup behind the check runs off the event loop.
    Every caller is a request handler, and a synchronous lookup against an
    attacker-chosen hostname would stall the whole worker.
    """
    normalized = url.strip().rstrip("/")
    if not normalized:
        raise api_error(400, "Endpoint URL is required", ErrorType.BAD_REQUEST)
    await aassert_public_url(
        normalized,
        allow_http=settings.environment in {"local", "test"},
    )
    return normalized


def user_model_is_on_the_clock(model: UserProvidedModel, now: datetime) -> bool:
    if not model.enabled or model.status != "active" or not model.online:
        return False
    if model.heartbeat_expires_at is None:
        return True
    try:
        expires_at = datetime.fromisoformat(
            model.heartbeat_expires_at.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(UTC) < expires_at.astimezone(UTC)


def dispatch_budget(kind: str) -> DispatchBudget:
    try:
        return _DISPATCH_BUDGETS[kind]
    except KeyError as exc:
        raise ValueError("invalid_user_model_kind") from exc


def sign_request_body(
    secret: str,
    body_bytes: bytes,
    now: datetime | int | float,
) -> str:
    """Build ``TR-Signature`` as ``t=<unix>,v1=HMAC(secret, t.body)``.

    The timestamp is integral Unix seconds and the body is signed byte-for-byte;
    callers must send the same serialized bytes they pass here.
    """
    timestamp = int(now.timestamp()) if isinstance(now, datetime) else int(now)
    message = str(timestamp).encode("ascii") + b"." + body_bytes
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"
