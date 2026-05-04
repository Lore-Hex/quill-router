"""/console/routing — auto-rollover model order and per-region endpoints."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.regions import configured_regions, region_payload
from trusted_router.routes.console._shared import ConsoleDep, render


def register(app: FastAPI) -> None:
    @app.get("/console/routing")
    async def console_routing(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        regions = []
        for region in region_payload(settings):
            # Strip the scheme + /v1 from api_base_url so the routing
            # page can show a bare hostname. region_payload already
            # routes the primary region to the canonical hostname.
            host = (
                region["api_base_url"]
                .removeprefix("https://")
                .removeprefix("http://")
                .removesuffix("/v1")
            )
            regions.append({
                "id": region["id"],
                "primary": region["primary"],
                "hostname": host,
            })
        auto_order = [item.strip() for item in settings.auto_model_order.split(",") if item.strip()]
        return HTMLResponse(render(
            "console/routing.html",
            settings=settings,
            user=ctx.user,
            active="routing",
            page_title="Routing",
            page_subtitle="Auto-rollover order and regional endpoints.",
            auto_order=auto_order,
            regions=regions,
            configured_regions=configured_regions(settings),
            api_base_url=settings.api_base_url,
        ))
