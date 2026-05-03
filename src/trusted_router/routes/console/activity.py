"""/console/activity — observability page: per-request metadata, no
prompt content."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.money import format_money_precise
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.storage import STORE


def register(app: FastAPI) -> None:
    @app.get("/console/activity")
    async def console_activity(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        events = STORE.activity_events(ctx.workspace.id, limit=50)
        for event in events:
            event["cost_display"] = format_money_precise(int(event.get("cost_microdollars") or 0))
        return HTMLResponse(render(
            "console/activity.html",
            settings=settings,
            user=ctx.user,
            active="activity",
            page_title="Observability",
            page_subtitle="Per-request metadata, no prompt content.",
            activity=events,
            api_base_url=settings.api_base_url,
        ))
