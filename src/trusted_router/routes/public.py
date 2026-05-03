from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from trusted_router.config import Settings
from trusted_router.dashboard import STATIC_DIR, dashboard_html
from trusted_router.og import OG_PNG_PATH
from trusted_router.trust import gcp_release, trust_html


def register_public_routes(app: FastAPI, settings: Settings) -> None:
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request) -> str:
        host = request.headers.get("host", "")
        if host.split(":", 1)[0].lower() == "trust.trustedrouter.com":
            return trust_html(settings)
        return dashboard_html(settings)

    @app.get("/trust", response_class=HTMLResponse)
    async def trust_page() -> str:
        return trust_html(settings)

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
