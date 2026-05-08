from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
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
STATUS_RECENT_SAMPLE_LIMIT = 5_000
STATUS_ROLLUP_LIMIT = 20_000
_STATUS_CACHE: tuple[float, dict[str, Any]] | None = None


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
    async def dashboard(request: Request) -> str:
        host = request.headers.get("host", "")
        hostname = host.split(":", 1)[0].lower()
        if hostname == "trust.trustedrouter.com":
            return trust_html(settings)
        if hostname == "status.trustedrouter.com":
            return _status_page_html(settings, host=hostname)
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
    async def status_page(request: Request) -> str:
        return _status_page_html(settings, host=request.headers.get("host", ""))

    @app.get("/status.json")
    async def status_json() -> JSONResponse:
        return JSONResponse(
            {"data": _status_snapshot(settings)},
            headers={"cache-control": "max-age=15, public"},
        )

    @app.get("/status/history")
    async def status_history(window: str = "48h") -> JSONResponse:
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
        samples = _status_samples(hours=48 if window in {"24h", "48h", "daily"} else 1)
        rollups = _status_rollups(window)
        return JSONResponse(
            {"data": history_payload(samples, window, rollups=rollups)},
            headers={"cache-control": "max-age=15, public"},
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


def _status_snapshot(settings: Settings) -> dict[str, Any]:
    global _STATUS_CACHE
    now = time.monotonic()
    if settings.environment != "test" and _STATUS_CACHE is not None:
        cached_at, payload = _STATUS_CACHE
        if now - cached_at < STATUS_SNAPSHOT_CACHE_SECONDS:
            return payload
    payload = status_snapshot(_status_samples(hours=48), rollups=_status_rollups("snapshot"))
    if settings.environment != "test":
        _STATUS_CACHE = (now, payload)
    return payload


def _status_samples(*, hours: int = 48) -> list[Any]:
    if hours <= 1:
        return STORE.synthetic_probe_samples(limit=STATUS_RECENT_SAMPLE_LIMIT)
    samples = []
    for date in _dates_covering_recent_hours(hours=hours):
        samples.extend(STORE.synthetic_probe_samples(date=date, limit=STATUS_RAW_SAMPLE_LIMIT_PER_DAY))
    deduped = {sample.id: sample for sample in samples}
    return sorted(deduped.values(), key=lambda sample: sample.created_at, reverse=True)


def _status_rollups(window: str) -> list[Any]:
    if window == "snapshot":
        return [
            *STORE.synthetic_rollups(period="hour", limit=STATUS_ROLLUP_LIMIT),
            *STORE.synthetic_rollups(period="day", limit=STATUS_ROLLUP_LIMIT),
            *STORE.synthetic_rollups(period="month", limit=STATUS_ROLLUP_LIMIT),
        ]
    if window in {"24h", "48h"}:
        return STORE.synthetic_rollups(period="hour", limit=STATUS_ROLLUP_LIMIT)
    if window == "daily":
        return STORE.synthetic_rollups(period="day", limit=STATUS_ROLLUP_LIMIT)
    if window == "monthly":
        return STORE.synthetic_rollups(period="month", limit=STATUS_ROLLUP_LIMIT)
    return []


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
