from __future__ import annotations

import datetime as dt
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import BackgroundTasks, FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from trusted_router.config import Settings
from trusted_router.dashboard import (
    STATIC_DIR,
    dashboard_html,
    public_model_detail_html,
    public_model_not_found_html,
    public_models_html,
    public_page_html,
)
from trusted_router.og import OG_PNG_PATH
from trusted_router.storage import STORE
from trusted_router.storage_models import utcnow
from trusted_router.synthetic.status import history_payload, status_snapshot
from trusted_router.trust import gcp_release, trust_html
from trusted_router.views import render_template

STATUS_SNAPSHOT_CACHE_SECONDS = 15
STATUS_RAW_SAMPLE_LIMIT_PER_DAY = 35_000
STATUS_LIVE_SAMPLE_LIMIT = 500
STATUS_HOUR_ROLLUP_LIMIT = 5_000
STATUS_DAY_ROLLUP_LIMIT = 25_000
STATUS_MONTH_ROLLUP_LIMIT = 50
STATUS_ROLLUP_RETENTION_MONTHS = 24
STATUS_RESPONSE_CACHE_SECONDS = 60
STATUS_RESPONSE_STALE_SECONDS = 600
STATUS_HISTORY_CACHE_SECONDS = 300
STATUS_HISTORY_STALE_SECONDS = 1_800
_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None
_STATUS_RESPONSE_CACHE: dict[str, _CachedPublicBody] = {}
_STATUS_RESPONSE_REFRESHING: set[str] = set()
_STATUS_RESPONSE_CACHE_LOCK = threading.RLock()


@dataclass(frozen=True)
class _CachedPublicBody:
    cached_at: float
    body: bytes
    media_type: str
    cache_control: str


class _CachedStaticFiles(StaticFiles):
    """StaticFiles + a public 1-day Cache-Control header.

    The default StaticFiles ships no cache directive, which means every
    visit to the marketing page re-fetches every CSS/JS/SVG asset on
    cold-load. We hash-bust nothing today, so the conservative play is
    a 24-hour public cache — long enough to take the edge off Cloud Run
    bandwidth, short enough that a deploy reaches users within a day."""

    def __init__(self, *args: Any, max_age: int = 86_400, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._max_age = max_age

    def file_response(
        self,
        full_path: Any,
        stat_result: Any,
        scope: Scope,
        status_code: int = 200,
    ) -> Response:
        response = super().file_response(full_path, stat_result, scope, status_code=status_code)
        response.headers.setdefault("cache-control", f"public, max-age={self._max_age}")
        return response


def register_public_routes(app: FastAPI, settings: Settings) -> None:
    app.mount("/static", _CachedStaticFiles(directory=STATIC_DIR), name="static")

    def public_html_route(
        path: str, *, include_slash: bool = True
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            app.api_route(path, methods=["GET", "HEAD"], response_class=HTMLResponse)(func)
            if include_slash and not path.endswith("/"):
                app.api_route(
                    f"{path}/",
                    methods=["GET", "HEAD"],
                    response_class=HTMLResponse,
                    include_in_schema=False,
                )(func)
            return func

        return decorator

    @public_html_route("/", include_slash=False)
    async def dashboard(request: Request, background_tasks: BackgroundTasks) -> Any:
        host = request.headers.get("host", "")
        hostname = host.split(":", 1)[0].lower()
        if hostname == "trust.trustedrouter.com":
            return trust_html(settings)
        if hostname == "status.trustedrouter.com":
            return _cached_status_page_response(
                settings,
                host=hostname,
                background_tasks=background_tasks,
            )
        return dashboard_html(settings)

    @public_html_route("/trust")
    async def trust_page() -> str:
        return trust_html(settings)

    @public_html_route("/compare/openrouter")
    async def compare_openrouter() -> str:
        return public_page_html(settings, "compare/openrouter")

    @public_html_route("/compare/vercel-ai-gateway")
    async def compare_vercel_ai_gateway() -> str:
        return public_page_html(settings, "compare/vercel-ai-gateway")

    @public_html_route("/compare/litellm")
    async def compare_litellm() -> str:
        return public_page_html(settings, "compare/litellm")

    @public_html_route("/docs/migrate-from-openrouter")
    async def migrate_from_openrouter() -> str:
        return public_page_html(settings, "docs/migrate-from-openrouter")

    @public_html_route("/security")
    async def security() -> str:
        return public_page_html(settings, "security")

    @public_html_route("/status")
    async def status_page(request: Request, background_tasks: BackgroundTasks) -> Response:
        return _cached_status_page_response(
            settings,
            host=request.headers.get("host", ""),
            background_tasks=background_tasks,
        )

    @app.get("/status.json")
    async def status_json(background_tasks: BackgroundTasks) -> Response:
        return _cached_public_response(
            settings,
            key="status:json",
            media_type="application/json",
            ttl_seconds=STATUS_RESPONSE_CACHE_SECONDS,
            stale_seconds=STATUS_RESPONSE_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: _json_body({"data": _status_snapshot(settings)}),
        )

    @app.get("/status/history")
    async def status_history(
        request: Request,
        background_tasks: BackgroundTasks,
        window: str = "48h",
        response_format: str | None = Query(default=None, alias="format"),
    ) -> Response:
        if window not in {"5m", "24h", "48h", "daily", "monthly"}:
            return JSONResponse(
                {
                    "error": {
                        "message": "window must be 5m, 24h, 48h, daily, or monthly",
                        "type": "bad_request",
                    }
                },
                status_code=400,
            )
        if not _wants_history_html(request, explicit_format=response_format):
            return _cached_public_response(
                settings,
                key=f"status:history:{window}:json",
                media_type="application/json",
                ttl_seconds=STATUS_HISTORY_CACHE_SECONDS,
                stale_seconds=STATUS_HISTORY_STALE_SECONDS,
                background_tasks=background_tasks,
                build=lambda: _json_body({"data": _status_history_payload(window)}),
            )
        return _cached_public_response(
            settings,
            key=f"status:history:{window}:html:{request.headers.get('host', '')}",
            media_type="text/html",
            ttl_seconds=STATUS_HISTORY_CACHE_SECONDS,
            stale_seconds=STATUS_HISTORY_STALE_SECONDS,
            background_tasks=background_tasks,
            build=lambda: _status_history_page_html(
                settings,
                host=request.headers.get("host", ""),
                window=window,
                history=_status_history_payload(window),
            ).encode(),
        )

    @public_html_route("/models")
    async def models() -> str:
        return public_models_html(settings)

    # Per-model detail page. Path captures `{author}/{slug}` (e.g.
    # `z-ai/glm-4.6`, `moonshotai/kimi-k2.6`) so the URL exactly mirrors
    # the OpenRouter model id. The `:path` converter lets the slash
    # through. Unknown ids render a styled 404 page (HTML, same chrome
    # as the rest of the marketing site) instead of FastAPI's default
    # JSON error body.
    @app.api_route(
        "/models/{model_id:path}",
        methods=["GET", "HEAD"],
        response_class=HTMLResponse,
    )
    async def model_detail(model_id: str) -> HTMLResponse:
        cleaned = model_id.strip()
        body = public_model_detail_html(settings, cleaned)
        if body is None:
            return HTMLResponse(
                public_model_not_found_html(settings, cleaned),
                status_code=404,
            )
        return HTMLResponse(body)

    @app.get("/og.png")
    async def og_image() -> FileResponse:
        return FileResponse(
            path=OG_PNG_PATH,
            media_type="image/png",
            headers={"cache-control": "max-age=3600, public"},
        )

    @app.get("/trust/gcp-release.json")
    async def trust_release() -> JSONResponse:
        return JSONResponse(gcp_release(settings), headers={"cache-control": "max-age=60, public"})

    @app.get("/trust/image-digest-gcp.txt")
    async def trust_digest() -> PlainTextResponse:
        return PlainTextResponse(
            f"{settings.trust_gcp_image_digest or 'not-configured'}\n",
            headers={"cache-control": "max-age=60, public"},
        )

    @app.get("/trust/image-reference-gcp.txt")
    async def trust_image_reference() -> PlainTextResponse:
        return PlainTextResponse(
            f"{settings.trust_gcp_image_reference or 'not-configured'}\n",
            headers={"cache-control": "max-age=60, public"},
        )


def _cached_status_page_response(
    settings: Settings,
    *,
    host: str,
    background_tasks: BackgroundTasks,
) -> Response:
    return _cached_public_response(
        settings,
        key=f"status:page:{host}",
        media_type="text/html",
        ttl_seconds=STATUS_RESPONSE_CACHE_SECONDS,
        stale_seconds=STATUS_RESPONSE_STALE_SECONDS,
        background_tasks=background_tasks,
        build=lambda: _status_page_html(settings, host=host).encode(),
    )


def _cached_public_response(
    settings: Settings,
    *,
    key: str,
    media_type: str,
    ttl_seconds: int,
    stale_seconds: int,
    background_tasks: BackgroundTasks,
    build: Callable[[], bytes],
) -> Response:
    cache_control = _public_cache_control(ttl_seconds=ttl_seconds, stale_seconds=stale_seconds)
    if settings.environment == "test":
        return Response(
            content=build(),
            media_type=media_type,
            headers={"cache-control": cache_control, "x-tr-cache": "bypass"},
        )

    now = time.monotonic()
    with _STATUS_RESPONSE_CACHE_LOCK:
        cached = _STATUS_RESPONSE_CACHE.get(key)
        if cached is not None:
            age = now - cached.cached_at
            if age < ttl_seconds:
                return _cached_body_response(cached, cache_state="hit")
            if age < ttl_seconds + stale_seconds:
                _schedule_cached_response_refresh(
                    key=key,
                    media_type=media_type,
                    cache_control=cache_control,
                    build=build,
                    background_tasks=background_tasks,
                )
                return _cached_body_response(cached, cache_state="stale")

    body = build()
    cached = _CachedPublicBody(
        cached_at=time.monotonic(),
        body=body,
        media_type=media_type,
        cache_control=cache_control,
    )
    with _STATUS_RESPONSE_CACHE_LOCK:
        _STATUS_RESPONSE_CACHE[key] = cached
    return _cached_body_response(cached, cache_state="miss")


def _schedule_cached_response_refresh(
    *,
    key: str,
    media_type: str,
    cache_control: str,
    build: Callable[[], bytes],
    background_tasks: BackgroundTasks,
) -> None:
    _ = background_tasks
    with _STATUS_RESPONSE_CACHE_LOCK:
        if key in _STATUS_RESPONSE_REFRESHING:
            return
        _STATUS_RESPONSE_REFRESHING.add(key)
    refresh_thread = threading.Thread(
        target=_refresh_cached_response,
        args=(key, media_type, cache_control, build),
        daemon=True,
    )
    refresh_thread.start()


def _refresh_cached_response(
    key: str,
    media_type: str,
    cache_control: str,
    build: Callable[[], bytes],
) -> None:
    try:
        body = build()
        with _STATUS_RESPONSE_CACHE_LOCK:
            _STATUS_RESPONSE_CACHE[key] = _CachedPublicBody(
                cached_at=time.monotonic(),
                body=body,
                media_type=media_type,
                cache_control=cache_control,
            )
    finally:
        with _STATUS_RESPONSE_CACHE_LOCK:
            _STATUS_RESPONSE_REFRESHING.discard(key)


def _cached_body_response(cached: _CachedPublicBody, *, cache_state: str) -> Response:
    return Response(
        content=cached.body,
        media_type=cached.media_type,
        headers={
            "cache-control": cached.cache_control,
            "x-tr-cache": cache_state,
        },
    )


def _public_cache_control(*, ttl_seconds: int, stale_seconds: int) -> str:
    browser_ttl = min(ttl_seconds, 15)
    return (
        f"public, max-age={browser_ttl}, s-maxage={ttl_seconds}, "
        f"stale-while-revalidate={stale_seconds}"
    )


def _json_body(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()


def _status_history_payload(window: str) -> dict[str, Any]:
    return history_payload(_status_samples(hours=1), window, rollups=_status_rollups(window))


def _wants_history_html(request: Request, *, explicit_format: str | None) -> bool:
    if explicit_format == "html":
        return True
    if explicit_format == "json":
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def _status_history_page_html(
    settings: Settings,
    *,
    host: str,
    window: str,
    history: dict[str, Any],
) -> str:
    hostname = host.split(":", 1)[0].lower()
    site_url = (
        f"https://status.trustedrouter.com/status/history?window={window}"
        if hostname == "status.trustedrouter.com"
        else f"https://{settings.trusted_domain}/status/history?window={window}"
    )
    title = {
        "48h": "48-hour Status History - TrustedRouter",
        "monthly": "Monthly Status History - TrustedRouter",
        "daily": "Daily Status History - TrustedRouter",
        "24h": "24-hour Status History - TrustedRouter",
        "5m": "Current Status History - TrustedRouter",
    }[window]
    heading = {
        "48h": "48-hour status history",
        "monthly": "Monthly status history",
        "daily": "Daily status history",
        "24h": "24-hour status history",
        "5m": "Current status history",
    }[window]
    return render_template(
        "public/status_history.html",
        api_base_url=settings.api_base_url,
        site_url=site_url,
        title=title,
        heading=heading,
        description="Visual rollups from metadata-only synthetic checks.",
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=settings.release,
        snapshot=_status_snapshot(settings),
        history=history,
        window=window,
        json_url=f"/status/history?window={window}&format=json",
    )


def _status_snapshot(settings: Settings) -> dict[str, Any]:
    global _STATUS_CACHE
    now = time.monotonic()
    if settings.environment != "test" and _STATUS_CACHE is not None:
        cached_at, payload = _STATUS_CACHE
        if now - cached_at < STATUS_SNAPSHOT_CACHE_SECONDS:
            return payload
    # Keep the public status hot path bounded: current state and headline
    # latency come from a small live sample window, while 24h/48h/monthly
    # history comes from compact rollups precomputed when the monitor writes
    # each sample. Do not scan raw 48h/day Bigtable rows on page load.
    payload = status_snapshot(_status_samples(hours=1), rollups=_status_rollups("snapshot"))
    if settings.environment != "test":
        _STATUS_CACHE = (now, payload)
    return payload


def _status_samples(*, hours: int = 48) -> list[Any]:
    if hours <= 1:
        return STORE.synthetic_probe_samples(limit=STATUS_LIVE_SAMPLE_LIMIT)
    samples = []
    for date in _dates_covering_recent_hours(hours=hours):
        samples.extend(STORE.synthetic_probe_samples(date=date, limit=STATUS_RAW_SAMPLE_LIMIT_PER_DAY))
    deduped = {sample.id: sample for sample in samples}
    return sorted(deduped.values(), key=lambda sample: sample.created_at, reverse=True)


def _status_rollups(window: str) -> list[Any]:
    now = utcnow()
    if window == "snapshot":
        return [
            *STORE.synthetic_rollups(
                period="hour",
                since=_hour_rollup_since(now, hours=48),
                limit=STATUS_HOUR_ROLLUP_LIMIT,
            ),
        ]
    if window in {"24h", "48h"}:
        return STORE.synthetic_rollups(
            period="hour",
            since=_hour_rollup_since(now, hours=24 if window == "24h" else 48),
            limit=STATUS_HOUR_ROLLUP_LIMIT,
        )
    if window == "daily":
        return STORE.synthetic_rollups(
            period="day",
            since=_day_rollup_since(now, months=STATUS_ROLLUP_RETENTION_MONTHS),
            limit=STATUS_DAY_ROLLUP_LIMIT,
        )
    if window == "monthly":
        return STORE.synthetic_rollups(
            period="day",
            since=_day_rollup_since(now, months=STATUS_ROLLUP_RETENTION_MONTHS),
            include_histograms=False,
            limit=STATUS_MONTH_ROLLUP_LIMIT,
        )
    return []


def _hour_rollup_since(now: dt.datetime, *, hours: int) -> str:
    base = now.astimezone(dt.UTC).replace(minute=0, second=0, microsecond=0)
    return _iso_utc(base - dt.timedelta(hours=max(hours - 1, 0)))


def _day_rollup_since(now: dt.datetime, *, months: int) -> str:
    return _iso_utc(_month_floor(now, months=months))


def _month_rollup_since(now: dt.datetime, *, months: int) -> str:
    return _iso_utc(_month_floor(now, months=months))


def _month_floor(now: dt.datetime, *, months: int) -> dt.datetime:
    current = now.astimezone(dt.UTC)
    month_index = current.year * 12 + current.month - 1
    cutoff_index = month_index - max(months - 1, 0)
    year, zero_based_month = divmod(cutoff_index, 12)
    return dt.datetime(year, zero_based_month + 1, 1, tzinfo=dt.UTC)


def _iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _dates_covering_recent_hours(*, hours: int) -> list[str]:
    now = utcnow()
    cutoff = now - dt.timedelta(hours=hours)
    dates = []
    current = cutoff.date()
    while current <= now.date():
        dates.append(current.isoformat())
        current += dt.timedelta(days=1)
    return dates


def _status_page_html(settings: Settings, *, host: str) -> str:
    hostname = host.split(":", 1)[0].lower()
    site_url = (
        "https://status.trustedrouter.com/"
        if hostname == "status.trustedrouter.com"
        else f"https://{settings.trusted_domain}/status"
    )
    snapshot = _status_snapshot(settings)
    return render_template(
        "public/status.html",
        api_base_url=settings.api_base_url,
        site_url=site_url,
        title="Status - TrustedRouter",
        heading="TrustedRouter Status",
        description="Regional uptime, attestation, SDK, billing, and fallback checks.",
        google_enabled=settings.google_oauth_enabled,
        github_enabled=settings.github_oauth_enabled,
        static_version=settings.release,
        snapshot=snapshot,
    )
