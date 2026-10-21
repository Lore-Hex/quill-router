"""/console/welcome — one-time key reveal landing for first sign-in."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.routes.oauth import PENDING_REVEAL_COOKIE


def register(app: FastAPI) -> None:
    @app.get("/console/welcome")
    def console_welcome(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
        first: int | None = None,
    ) -> Response:
        # `tr_pending_reveal` is the short-lived one-shot cookie set by
        # the OAuth callback right before its 302 here. It carries the
        # raw API key minted during STORE.signup(). We read it ONCE,
        # render it, and clear it on the response. The path scope
        # (/console/welcome) prevents it from echoing on any other URL,
        # and clearing it means a refresh of this page after the reveal
        # falls through to the static "already displayed" copy — the
        # exact one-shot semantics the panel-head text advertises.
        revealed_key: str | None = None
        clear_pending_reveal = False
        if first is not None:
            cookie_value = request.cookies.get(PENDING_REVEAL_COOKIE)
            if cookie_value:
                revealed_key = cookie_value
                clear_pending_reveal = True
        response = HTMLResponse(render(
            "console/welcome.html",
            settings=settings,
            ctx=ctx,
            active="api-keys",
            page_title="Your API key",
            page_subtitle="",
            revealed_key=revealed_key,
            workspace_name=ctx.workspace.name,
        ))
        if clear_pending_reveal:
            # Delete cookie with the same path it was set with — otherwise
            # the browser keeps the original and the next refresh re-reveals
            # the key (defeats the one-shot guarantee).
            response.delete_cookie(
                key=PENDING_REVEAL_COOKIE,
                path="/console/welcome",
                secure=settings.environment.lower() not in {"local", "test"},
                samesite="lax",
            )
        return response
