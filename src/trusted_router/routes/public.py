from __future__ import annotations

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
from trusted_router.synthetic.status import history_payload, status_snapshot
from trusted_router.trust import gcp_release, trust_html
from trusted_router.views import render_template


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
    async def status_history(window: str = "24h") -> JSONResponse:
        if window not in {"5m", "24h", "daily"}:
            return JSONResponse(
                {"error": {"message": "window must be 5m, 24h, or daily", "type": "bad_request"}},
                status_code=400,
            )
        samples = STORE.synthetic_probe_samples(limit=settings.synthetic_status_sample_limit)
        return JSONResponse(
            {"data": history_payload(samples, window)},
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
    samples = STORE.synthetic_probe_samples(limit=settings.synthetic_status_sample_limit)
    return status_snapshot(samples)


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
