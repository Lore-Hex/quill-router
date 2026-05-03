"""/internal/sentry-test — synthetic exception so an operator can
verify the Sentry integration end-to-end. 404'd in production unless
TR_ENABLE_SENTRY_TEST_ROUTE is set, so it's not a permanent surface."""

from __future__ import annotations

from fastapi import APIRouter, Request

from trusted_router.auth import SettingsDep
from trusted_router.errors import api_error
from trusted_router.routes.internal._shared import require_internal_gateway
from trusted_router.types import ErrorType


def register(router: APIRouter) -> None:
    @router.get("/internal/sentry-test")
    async def sentry_test(request: Request, settings: SettingsDep) -> None:
        if (
            settings.environment.lower() not in {"local", "test"}
            and not settings.enable_sentry_test_route
        ):
            raise api_error(404, "Resource not found", ErrorType.NOT_FOUND)
        require_internal_gateway(request, settings)
        raise RuntimeError("synthetic sentry test")
