"""Shared auth gate for /internal/* routes.

`require_internal_gateway` is the bearer-or-header check that guards
the gateway authorize/settle/refund triplet plus the sentry-test
synthetic. Production deploys must set TR_INTERNAL_GATEWAY_TOKEN; the
local/test escape hatch lets unit tests avoid wiring a token.
"""

from __future__ import annotations

from fastapi import Request

from trusted_router.auth import get_authorization_bearer
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.security import constant_time_equal
from trusted_router.types import ErrorType


def require_internal_gateway(request: Request, settings: Settings) -> None:
    if settings.internal_gateway_token:
        supplied = (
            get_authorization_bearer(request)
            or request.headers.get("x-trustedrouter-internal-token")
            or ""
        )
        if not constant_time_equal(supplied, settings.internal_gateway_token):
            raise api_error(401, "Invalid internal gateway token", ErrorType.UNAUTHORIZED)
        return
    if settings.environment not in {"local", "test"}:
        raise api_error(403, "Internal gateway token is required", ErrorType.FORBIDDEN)
