"""/console/account/preferences — sign-in provider, environment, sign-out."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.routes.console._shared import ConsoleDep, render


def register(app: FastAPI) -> None:
    @app.get("/console/account/preferences")
    async def console_preferences(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        return HTMLResponse(render(
            "console/account/preferences.html",
            settings=settings,
            user=ctx.user,
            active="preferences",
            page_title="Preferences",
            page_subtitle="Account and sign-in.",
            provider=ctx.session.provider,
            environment=settings.environment,
            api_base_url=settings.api_base_url,
        ))
