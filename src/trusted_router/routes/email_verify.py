"""Email verification landing page for wallet users.

When a wallet user signs in via MetaMask we send them a magic link that
points here. Clicking it consumes the one-shot token, marks the user's
email verified, and upgrades their pending session to active. The
response is HTML — they're in a browser tab from clicking a link in their
email — and 302s them on to the welcome page.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import (
    SESSION_COOKIE_NAME,
    SettingsDep,
    set_session_cookie,
)
from trusted_router.storage import STORE
from trusted_router.views import render_template


def register_email_verify_routes(router: APIRouter) -> None:
    @router.get("/auth/verify-email")
    async def verify_email(request: Request, token: str, settings: SettingsDep) -> Response:
        consumed = STORE.consume_verification_token(token, purpose="signup")
        if consumed is None:
            return HTMLResponse(_invalid_page(), status_code=400)

        marked = STORE.mark_user_email_verified(consumed.user_id)
        if marked is None:
            return HTMLResponse(_invalid_page(), status_code=400)

        # If the user has a pending-email session cookie from the wallet flow,
        # upgrade it in place so they're immediately logged in. Otherwise
        # fall back to a "your email is verified — sign in" landing.
        cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
        upgraded = STORE.upgrade_auth_session(cookie_token, state="active") if cookie_token else None
        if upgraded is None:
            return HTMLResponse(
                render_template("auth/verify_email_done.html", page_title="Email verified"),
                status_code=200,
            )

        response = RedirectResponse(url="/console/welcome?first=1", status_code=302)
        # Re-stamp the cookie so its max-age is reset alongside the state change.
        set_session_cookie(response, cookie_token or "", settings)
        return response


def _invalid_page() -> str:
    return render_template("auth/verify_email_invalid.html", page_title="Link expired")
