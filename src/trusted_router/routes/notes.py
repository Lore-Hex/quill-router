"""Public notes are rebuilt separately from the control plane."""
from __future__ import annotations

import re
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from starlette.responses import FileResponse, RedirectResponse, Response

_ORIGIN = "https://raw.githubusercontent.com/Lore-Hex/quill-router/published-notes"
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def _document(filename: str, media_type: str) -> Response:
    try:
        remote = httpx.get(f"{_ORIGIN}/{filename}", timeout=4.0)
        if remote.status_code == 404:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        remote.raise_for_status()
    except httpx.HTTPError:
        return Response(status_code=503, headers={"Retry-After": "60", "Cache-Control": "no-store"})
    return Response(remote.content, media_type=media_type, headers={
        "Cache-Control": "public, max-age=60, s-maxage=120",
        "Content-Security-Policy": "sandbox allow-popups allow-top-navigation-by-user-activation; default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'",
        "X-Content-Type-Options": "nosniff",
    })


def register_notes(app: FastAPI) -> None:
    @app.api_route("/openrouter", methods=["GET", "HEAD"], include_in_schema=False)
    def openrouter_notice() -> Response:
        page = Path(__file__).resolve().parents[1] / "templates/public/openrouter_notice.html"
        return FileResponse(page, media_type="text/html")

    @app.api_route("/notes", methods=["GET", "HEAD"], include_in_schema=False)
    def notes_archive() -> Response:
        return _document("index.html", "text/html")

    @app.api_route("/sitemap-notes.xml", methods=["GET", "HEAD"], include_in_schema=False)
    def notes_sitemap() -> Response:
        return _document("sitemap.xml", "application/xml")

    @app.api_route("/notes/{slug}", methods=["GET", "HEAD"], include_in_schema=False)
    def note(slug: str, request: Request) -> Response:
        if slug == "openrouter-minimax-prompt-notice":
            query = request.url.query
            return RedirectResponse("/openrouter" + (f"?{query}" if query else ""), status_code=308)
        if len(slug) > 160 or not _SLUG.fullmatch(slug):
            return Response(status_code=404)
        return _document(f"{slug}/index.html", "text/html")
