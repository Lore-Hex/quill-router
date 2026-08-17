"""/console/activity — observability page: per-request metadata, no
prompt content."""

from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import OrderedDict
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.client_reliability import availability
from trusted_router.config import Settings
from trusted_router.money import format_money_precise
from trusted_router.operational_analytics import OperationalAnalyticsClient
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.storage import STORE
from trusted_router.storage_operational_analytics import analytics_surrogate

_USAGE_CACHE_TTL_SECONDS = 60.0
_CLIENT_RELIABILITY_CACHE_TTL_SECONDS = 60.0
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
_CLIENT_RELIABILITY_CACHE = _UsageCache()
CLIENT_RELIABILITY_RANGE_PRESETS = {
    "1h": 60,
    "6h": 360,
    "24h": 1440,
    "7d": 10080,
}
log = logging.getLogger(__name__)


def _operational_analytics_client(
    settings: Settings,
) -> OperationalAnalyticsClient | None:
    if not (
        settings.operational_analytics_clickhouse_url
        and settings.operational_analytics_clickhouse_password
    ):
        return None
    return OperationalAnalyticsClient(
        base_url=settings.operational_analytics_clickhouse_url,
        user=settings.operational_analytics_clickhouse_user,
        password=settings.operational_analytics_clickhouse_password,
        database=settings.operational_analytics_clickhouse_database,
    )


def _percent(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator * 100, 2) if denominator else None


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def _top_errors(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str | None, int | None], int] = {}
    for row in rows:
        key = (row.get("first_error_class"), row.get("final_http_status"))
        grouped[key] = grouped.get(key, 0) + 1
    return [
        {"error_class": error_class, "http_status": http_status, "count": count}
        for (error_class, http_status), count in sorted(
            grouped.items(),
            key=lambda item: (-item[1], str(item[0][0] or ""), item[0][1] or 0),
        )
    ]


def _client_reliability_data(
    client: OperationalAnalyticsClient,
    *,
    workspace_id: str,
    range_: str,
    window_minutes: int,
) -> dict[str, Any]:
    tenant_id = analytics_surrogate("workspace", workspace_id)
    since = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=window_minutes)
    summary = client.client_reliability_summary(
        tenant_id,
        window_minutes=window_minutes,
    )
    failures = client.client_events_recent(tenant_id, since=since, limit=50)
    generations = client.activity_generations(
        tenant_id=tenant_id,
        start_at=since.isoformat().replace("+00:00", "Z"),
        limit=5001,
    )
    truncated = len(generations) > 5000
    generations = generations[:5000]
    elapsed = [
        generation.elapsed_milliseconds
        for generation in generations
        if generation.elapsed_milliseconds is not None
    ]
    requests = int(summary.get("requests") or 0)
    successes = int(summary.get("successes") or 0)
    tr_fault = int(summary.get("tr_fault") or 0)
    measured = availability(successes, tr_fault)
    retry_count = min(requests, max(0, int(summary.get("attempts") or 0) - requests))
    rendered_summary = dict(summary)
    rendered_summary.update(
        {
            "availability_percent": (
                round(measured * 100, 4)
                if measured is not None and requests >= 100
                else None
            ),
            "retried_percent": _percent(retry_count, requests),
            "failover_percent": _percent(
                int(summary.get("failover_used") or 0),
                requests,
            ),
        }
    )
    return {
        "data": {
            "range": range_,
            "summary": rendered_summary,
            "server_p50_elapsed_ms": _median(elapsed),
            "top_errors": _top_errors(failures),
            "recent_failures": failures,
        },
        "meta": {
            "scanned": len(generations) + len(failures),
            "truncated": truncated,
            "freshness_seconds": 0,
        },
    }


def register(app: FastAPI) -> None:
    @app.get("/console/activity")
    async def console_activity(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        events = STORE.activity_events(ctx.workspace.id, limit=50)
        for event in events:
            event["cost_display"] = format_money_precise(int(event.get("cost_microdollars") or 0))
        return HTMLResponse(render(
            "console/activity.html",
            settings=settings,
            ctx=ctx,
            active="activity",
            page_title="Observability",
            page_subtitle="Per-request metadata, no prompt content.",
            activity=events,
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

    @app.get("/console/activity/client-reliability.json")
    async def console_activity_client_reliability(
        ctx: ConsoleDep,
        settings: SettingsDep,
        range_: str = Query("24h", alias="range"),
    ) -> dict[str, Any]:
        if range_ not in CLIENT_RELIABILITY_RANGE_PRESETS:
            raise HTTPException(status_code=400, detail="invalid range")
        cache_key = (ctx.workspace.id, range_, False, "client_reliability")
        now = time.monotonic()
        cached = _CLIENT_RELIABILITY_CACHE.get(cache_key, now=now)
        if cached is not None:
            return cached
        client = _operational_analytics_client(settings)
        if client is None:
            result = {"data": None, "meta": {"reason": "unavailable"}}
        else:
            try:
                result = _client_reliability_data(
                    client,
                    workspace_id=ctx.workspace.id,
                    range_=range_,
                    window_minutes=CLIENT_RELIABILITY_RANGE_PRESETS[range_],
                )
            except Exception:
                log.exception("console_client_reliability_read_failed")
                result = {"data": None, "meta": {"reason": "unavailable"}}
        _CLIENT_RELIABILITY_CACHE.put(
            cache_key,
            result,
            expires_at=now + _CLIENT_RELIABILITY_CACHE_TTL_SECONDS,
        )
        return result
