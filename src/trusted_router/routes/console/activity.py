"""/console/activity — observability page: per-request metadata, no
prompt content."""

from __future__ import annotations

import time
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.money import format_money_precise
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.storage import STORE

_USAGE_CACHE_TTL_SECONDS = 60.0
_UsageCacheKey = tuple[str, int, str, bool, str | None]
_USAGE_CACHE: dict[_UsageCacheKey, tuple[float, dict[str, Any]]] = {}


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

    @app.get("/console/activity/usage.json")
    async def console_activity_usage(
        ctx: ConsoleDep,
        days: int = 30,
        granularity: str | None = None,
        by_model: bool = False,
        api_key_hash: str | None = None,
    ) -> dict[str, Any]:
        clamped_days = min(90, max(1, days))
        resolved_granularity = granularity or ("hour" if clamped_days <= 2 else "day")
        if resolved_granularity not in {"hour", "day"}:
            raise HTTPException(status_code=400, detail="granularity must be 'hour' or 'day'")
        cache_key = (
            ctx.workspace.id,
            clamped_days,
            resolved_granularity,
            by_model,
            api_key_hash,
        )
        now = time.monotonic()
        cached = _USAGE_CACHE.get(cache_key)
        if cached is not None and cached[0] > now:
            return cached[1]
        result = STORE.usage_series(
            ctx.workspace.id,
            days=clamped_days,
            granularity=resolved_granularity,
            api_key_hash=api_key_hash,
            by_model=by_model,
        )
        # A shared cache (Redis) is deferred; this per-worker TTL covers
        # occasional console reads and protects Bigtable from refresh bursts.
        _USAGE_CACHE[cache_key] = (now + _USAGE_CACHE_TTL_SECONDS, result)
        return result
