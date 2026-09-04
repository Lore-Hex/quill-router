"""Shared auth gate for /internal/* routes.

`require_internal_gateway` selects the credential owned by the route, not just
the process. Billing routes accept only TR_INTERNAL_GATEWAY_TOKEN, while
synthetic/Sentry routes accept only TR_OBSERVER_INTERNAL_TOKEN even when they
are mounted on the internal service. The sole temporary exception is the
explicit deployed-combined bridge: its pre-#714 monitor jobs still present the
legacy gateway token until #712 completes the split. The local/test escape
hatch lets unit tests avoid wiring a token.
"""

from __future__ import annotations

from fastapi import Request

from trusted_router.auth import get_authorization_bearer
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.security import constant_time_equal
from trusted_router.types import ErrorType

_OBSERVER_INTERNAL_EXACT_PATHS = frozenset({"/internal/sentry-test"})
_OBSERVER_INTERNAL_PATH_PREFIXES = ("/internal/synthetic/",)


def internal_service_credential(
    settings: Settings,
    path: str,
) -> tuple[str, str | None]:
    """Return the credential class and configured secret for an internal path."""
    normalized_path = path[3:] if path.startswith("/v1/internal/") else path
    from trusted_router.routes.internal.admin import is_operator_route

    if is_operator_route(normalized_path):
        return "operator", settings.operator_token
    observer_path = normalized_path in _OBSERVER_INTERNAL_EXACT_PATHS or normalized_path.startswith(
        _OBSERVER_INTERNAL_PATH_PREFIXES
    )
    legacy_combined_bridge = (
        settings.service_surface == "combined"
        and settings.allow_deployed_combined_surface
    )
    if observer_path and not legacy_combined_bridge:
        return "observer", settings.observer_internal_token
    return "gateway", settings.internal_gateway_token


def require_internal_gateway(request: Request, settings: Settings) -> None:
    _kind, expected = internal_service_credential(settings, request.url.path)
    if expected:
        supplied = (
            get_authorization_bearer(request)
            or request.headers.get("x-trustedrouter-internal-token")
            or ""
        )
        if not constant_time_equal(supplied, expected):
            raise api_error(401, "Invalid internal service token", ErrorType.UNAUTHORIZED)
        return
    if settings.environment not in {"local", "test"}:
        raise api_error(403, "Internal service token is required", ErrorType.FORBIDDEN)
