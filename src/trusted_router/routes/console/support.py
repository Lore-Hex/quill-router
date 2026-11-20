"""Session-authenticated counterpart to the anonymous support form."""

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from trusted_router.auth import SettingsDep
from trusted_router.routes.console._shared import ConsoleDep
from trusted_router.routes.console.social_rewards import _same_origin
from trusted_router.routes.public import _handle_support_inquiry


def register(app: FastAPI) -> None:
    @app.post("/console/support/inquiry", include_in_schema=False)
    async def support_inquiry(request: Request, ctx: ConsoleDep, settings: SettingsDep) -> JSONResponse:
        _same_origin(request, settings)
        user = ctx.user
        if user.disabled or user.suspended or user.account_closed_at:
            raise HTTPException(403, "Use the signed-out support form.")
        return await _handle_support_inquiry(
            settings, request, verified_account_email=user.email if user.email_verified else None,
        )
