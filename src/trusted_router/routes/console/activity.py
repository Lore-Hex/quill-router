"""/console/activity — observability page: per-request metadata, no
prompt content."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.money import format_money_precise
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.storage import STORE

_USAGE_CACHE_TTL_SECONDS = 60.0
USAGE_RANGE_PRESETS: dict[str, tuple[int, str]] = {
    "1h": (60, "minute"),
    "6h": (360, "5min"),
    "24h": (1440, "hour"),
    "7d": (10080, "day"),
    "30d": (43200, "day"),
    "90d": (129600, "day"),
}
_UsageCacheKey = tuple[str, str, bool, str | None]
_USAGE_CACHE_MAX_ENTRIES = 256


class _UsageCache:
    """Small bounded TTL cache for expensive console usage queries."""

    def __init__(self, *, max_entries: int = _USAGE_CACHE_MAX_ENTRIES) -> None:
        if max_entries < 1:
            raise ValueError("usage cache max_entries must be positive")
        self._max_entries = max_entries
        self._entries: OrderedDict[
            _UsageCacheKey, tuple[float, dict[str, Any]]
        ] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: _UsageCacheKey, *, now: float) -> dict[str, Any] | None:
        with self._lock:
            cached = self._entries.get(key)
            if cached is None:
                return None
            if cached[0] <= now:
                self._entries.pop(key, None)
                return None
            self._entries.move_to_end(key)
            return cached[1]

    def put(
        self,
        key: _UsageCacheKey,
        value: dict[str, Any],
        *,
        expires_at: float,
    ) -> None:
        with self._lock:
            self._entries[key] = (expires_at, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_USAGE_CACHE = _UsageCache()


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
        range_: str = Query("30d", alias="range"),
        by_model: bool = False,
        api_key_hash: str | None = None,
    ) -> dict[str, Any]:
        if range_ not in USAGE_RANGE_PRESETS:
            raise HTTPException(status_code=400, detail="invalid range")
        window_minutes, granularity = USAGE_RANGE_PRESETS[range_]
        cache_key = (
            ctx.workspace.id,
            range_,
            by_model,
            api_key_hash,
        )
        now = time.monotonic()
        cached = _USAGE_CACHE.get(cache_key, now=now)
        if cached is not None:
            return cached
        result = STORE.usage_series(
            ctx.workspace.id,
            window_minutes=window_minutes,
            granularity=granularity,
            api_key_hash=api_key_hash,
            by_model=by_model,
        )
        result = dict(result)
        result["range"] = range_
        latest = STORE.activity_events(
            ctx.workspace.id,
            api_key_hash=api_key_hash,
            limit=1,
        )
        result["latest_activity_at"] = latest[0].get("created_at") if latest else None
        # A shared cache (Redis) is deferred; this per-worker TTL covers
        # occasional console reads and protects Bigtable from refresh bursts.
        _USAGE_CACHE.put(
            cache_key,
            result,
            expires_at=now + _USAGE_CACHE_TTL_SECONDS,
        )
        return result
