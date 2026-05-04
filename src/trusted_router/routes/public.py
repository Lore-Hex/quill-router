from __future__ import annotations

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
    public_models_html,
    public_page_html,
)
from trusted_router.og import OG_PNG_PATH
from trusted_router.trust import gcp_release, trust_html


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

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> str:
        host = request.headers.get("host", "")
        if host.split(":", 1)[0].lower() == "trust.trustedrouter.com":
            return trust_html(settings)
        return dashboard_html(settings)

    @app.get("/trust", response_class=HTMLResponse)
    async def trust_page() -> str:
        return trust_html(settings)

    @app.get("/compare/openrouter", response_class=HTMLResponse)
    async def compare_openrouter() -> str:
        return public_page_html(settings, "compare/openrouter")

    @app.get("/compare/vercel-ai-gateway", response_class=HTMLResponse)
    async def compare_vercel_ai_gateway() -> str:
        return public_page_html(settings, "compare/vercel-ai-gateway")

    @app.get("/compare/litellm", response_class=HTMLResponse)
    async def compare_litellm() -> str:
        return public_page_html(settings, "compare/litellm")

    @app.get("/docs/migrate-from-openrouter", response_class=HTMLResponse)
    async def migrate_from_openrouter() -> str:
        return public_page_html(settings, "docs/migrate-from-openrouter")

    @app.get("/security", response_class=HTMLResponse)
    async def security() -> str:
        return public_page_html(settings, "security")

    @app.get("/models", response_class=HTMLResponse)
    async def models() -> str:
        return public_models_html(settings)

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
