from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from trusted_router.catalog import MODELS, PROVIDERS
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.services.safe_egress import assert_public_url
from trusted_router.storage_custom_models import custom_model_id_from_slug, custom_model_slug
from trusted_router.storage_models import UserProvidedModel
from trusted_router.types import ErrorType

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


def reserved_user_model_names() -> frozenset[str]:
    return _RESERVED_NAMES


def validate_user_model_slug(slug: str) -> str:
    model_id = custom_model_id_from_slug(slug)
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


def validate_endpoint_url(url: str, settings: Settings) -> str:
    normalized = url.strip().rstrip("/")
    if not normalized:
        raise api_error(400, "Endpoint URL is required", ErrorType.BAD_REQUEST)
    assert_public_url(
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
