"""/console/welcome — one-time key reveal landing for first sign-in."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.routes.console._shared import ConsoleDep, money, render
from trusted_router.routes.oauth import PENDING_REVEAL_COOKIE
from trusted_router.typed_balance import live_credit_summary


def register(app: FastAPI) -> None:
    @app.get("/console/welcome")
    async def console_welcome(
        request: Request,
        ctx: ConsoleDep,
        settings: SettingsDep,
        first: int | None = None,
    ) -> Response:
        summary = live_credit_summary(ctx.workspace.id)
        trial_microdollars = summary["total_credits"] if summary else 0
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
            user=ctx.user,
            active="api-keys",
            page_title="Welcome",
            page_subtitle="Save your API key — it won't be shown again.",
            revealed_key=revealed_key,
            workspace_name=ctx.workspace.name,
            # `trial_credit` is the formatted display value; the matching raw
            # amount lets the template distinguish starter-credit accounts
            # from accounts configured with the grant disabled.
            trial_credit=money(trial_microdollars),
            trial_credit_microdollars=trial_microdollars,
            api_base_url=settings.api_base_url,
        ))
        if clear_pending_reveal:
            # Delete cookie with the same path it was set with — otherwise
            # the browser keeps the original and the next refresh re-reveals
            # the key (defeats the one-shot guarantee).
            response.delete_cookie(
                key=PENDING_REVEAL_COOKIE,
                path="/console/welcome",
                secure=settings.environment.lower() == "production",
                samesite="lax",
            )
        return response
