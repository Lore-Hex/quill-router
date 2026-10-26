"""/console/account/preferences — sign-in provider, environment, sign-out."""

from __future__ import annotations

import hashlib
import hmac

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep, clear_session_cookie
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.routes.console.social_rewards import _same_origin
from trusted_router.storage import STORE, AuthSession
from trusted_router.storage_account_closure import AccountClosureBlocked
from trusted_router.views import render_template

_HEADERS = {"Cache-Control": "private, no-store", "Vary": "Cookie", "X-Robots-Tag": "noindex"}


def _closure_token(session: AuthSession) -> str:
    return hmac.new(
        session.secret_hash.encode(), f"account-closure:{session.hash}:{session.user_id}".encode(), hashlib.sha256,
    ).hexdigest()


def register(app: FastAPI) -> None:
    @app.get("/console/account/preferences")
    def console_preferences(ctx: ConsoleDep, settings: SettingsDep) -> Response:
        return HTMLResponse(render(
            "console/account/preferences.html",
            settings=settings,
            ctx=ctx,
            active="preferences",
            page_title="Preferences",
            page_subtitle="Account and sign-in.",
            provider=ctx.session.provider,
            environment=settings.environment,
        ))

    @app.get("/console/account/delete")
    def confirm_account_deletion(ctx: ConsoleDep, settings: SettingsDep, error: str = "") -> Response:
        messages = {
            "confirmation": "Type DELETE and confirm the account closure to continue.",
            "shared": "Your account belongs to a shared workspace. Contact support to close it without interrupting your team.",
            "assisted": "This account needs assisted closure. Contact support and we will help you close it.",
        }
        return HTMLResponse(render(
            "console/account/delete.html", settings=settings, ctx=ctx, active="preferences",
            page_title="Delete account", page_subtitle="Close your account and stop new usage.",
            closure_token=_closure_token(ctx.session), error=messages.get(error, ""),
        ), headers=_HEADERS)

    @app.post("/console/account/delete")
    def delete_account(
        request: Request, ctx: ConsoleDep, settings: SettingsDep,
        csrf_token: str = Form(""), confirmation: str = Form(""), acknowledge: str = Form(""),
    ) -> Response:
        _same_origin(request, settings)
        if not hmac.compare_digest(csrf_token, _closure_token(ctx.session)):
            raise HTTPException(403, "Invalid confirmation. Reload the page and try again.")
        if confirmation != "DELETE" or acknowledge != "yes":
            return RedirectResponse("/console/account/delete?error=confirmation", status_code=303, headers=_HEADERS)
        try:
            closed = STORE.close_user_account(ctx.user.id)
        except AccountClosureBlocked as exc:
            code = "shared" if str(exc) == "shared" else "assisted"
            return RedirectResponse(f"/console/account/delete?error={code}", status_code=303, headers=_HEADERS)
        if closed is None:
            raise HTTPException(409, "Account unavailable. Contact support for assistance.")
        # Do not accept a user ID or email from the form: only this session's
        # user can be closed. All their sessions now fail the disabled gate.
        response = HTMLResponse(render_template("auth/account_closed.html", page_title="Account closed"), headers=_HEADERS)
        clear_session_cookie(response, settings)
        return response
