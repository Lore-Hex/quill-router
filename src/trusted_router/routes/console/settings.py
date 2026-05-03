"""/console/settings — workspace name, content storage status."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.routes.console._shared import ConsoleDep, render


def register(app: FastAPI) -> None:
    @app.get("/console/settings")
    async def console_settings(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        return HTMLResponse(render(
            "console/settings.html",
            settings=settings,
            user=ctx.user,
            active="settings",
            page_title="Workspace settings",
            page_subtitle="Names, content storage, integrations.",
            workspace=ctx.workspace,
            api_base_url=settings.api_base_url,
        ))
