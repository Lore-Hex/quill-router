"""/console/settings — workspace name, content storage status."""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router import phone_verification as pv
from trusted_router.auth import SettingsDep
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.services.notify import send_verification_code
from trusted_router.storage import STORE

MAX_WORKSPACE_NAME = 120


def _back(query: str) -> RedirectResponse:
    """Back to the settings page, POST-redirect-GET.

    Without the redirect a refresh re-posts the form, which for the start
    handler means ringing the visitor's phone again.
    """
    return RedirectResponse(
        url=f"/console/settings?{query}" if query else "/console/settings",
        status_code=303,
    )


def register(app: FastAPI) -> None:
    @app.get("/console/settings")
    async def console_settings(
        ctx: ConsoleDep,
        settings: SettingsDep,
        saved: str = "",
        error: str = "",
        sent: str = "",
        phone_saved: str = "",
        detail: str = "",
    ) -> Response:
        # Read the user fresh. ctx.user is the session's snapshot, so a number
        # verified moments ago on this same page would otherwise still render
        # as unverified and invite the visitor to start over.
        user = STORE.get_user(ctx.user.id) or ctx.user
        return HTMLResponse(render(
            "console/settings.html",
            settings=settings,
            ctx=ctx,
            active="settings",
            page_title="Workspace settings",
            page_subtitle="Names, content storage, integrations.",
            can_manage=STORE.user_can_manage(ctx.user.id, ctx.workspace.id),
            saved=bool(saved),
            error=error,
            phone=user.phone,
            phone_verified=bool(user.phone_verified),
            phone_pending=user.pending_phone,
            phone_sent=sent,
            phone_saved=bool(phone_saved),
            phone_error=error,
            phone_error_detail=detail,
        ))

    @app.post("/console/settings/phone/start")
    async def console_phone_start(
        ctx: ConsoleDep,
        settings: SettingsDep,
        phone: str = Form(""),
        channel: str = Form("voice"),
    ) -> Response:
        """Send a verification code to a number the visitor claims."""
        allowed, wait = pv.can_resend(ctx.user)
        if not allowed:
            # Without a floor, this form is a way to ring someone else's phone
            # repeatedly by typing their number.
            return _back(f"error=rate&detail={wait}s")

        try:
            normalized = pv.normalize_phone(phone)
        except pv.PhoneNumberError as exc:
            return _back(f"error=phone&detail={quote(str(exc))}")

        started = STORE.begin_phone_verification(ctx.user.id, normalized)
        if started is None:
            return _back("error=phone&detail=account+not+found")
        code, _updated = started

        wanted: Literal["sms", "voice"] = "sms" if channel == "sms" else "voice"
        delivered, detail = send_verification_code(settings, normalized, code, channel=wanted)
        if not delivered:
            # The pending code is left in place deliberately: a carrier blip is
            # not a rejected number, and clearing it would send the visitor
            # back to the start for nothing.
            return _back(f"error=send&detail={quote(detail[:120])}")
        return _back(f"sent={'a phone call' if wanted == 'voice' else 'text message'}")

    @app.post("/console/settings/phone/confirm")
    async def console_phone_confirm(ctx: ConsoleDep, code: str = Form("")) -> Response:
        status, _user = STORE.confirm_phone_verification(ctx.user.id, code)
        if status == "ok":
            return _back("phone_saved=1")
        return _back(f"error={status}")

    @app.post("/console/settings/phone/remove")
    async def console_phone_remove(ctx: ConsoleDep) -> Response:
        STORE.clear_user_phone(ctx.user.id)
        return _back("")

    @app.post("/console/settings")
    async def console_update_settings(
        ctx: ConsoleDep,
        name: str = Form(""),
    ) -> Response:
        """Rename the workspace.

        This handler did not exist. The template has posted here since the page
        was written, so every Save returned 405 "Method Not Allowed" and the
        rename silently never worked.
        """
        # Renaming is a management action: the name appears on invoices and in
        # every member's console, so a plain member must not be able to change
        # it. Same gate the API-key management routes use.
        if not STORE.user_can_manage(ctx.user.id, ctx.workspace.id):
            return RedirectResponse(url="/console/settings?error=forbidden", status_code=303)

        cleaned = name.strip()
        if not cleaned:
            # Reject rather than store a blank. An empty name renders as an
            # unidentifiable entry in the workspace switcher, and the only way
            # back is this same form.
            return RedirectResponse(url="/console/settings?error=name", status_code=303)
        if len(cleaned) > MAX_WORKSPACE_NAME:
            # The input carries maxlength, but that is a client-side hint, not a
            # constraint on anything posting directly.
            return RedirectResponse(url="/console/settings?error=too_long", status_code=303)

        if STORE.update_workspace(ctx.workspace.id, name=cleaned) is None:
            return RedirectResponse(url="/console/settings?error=missing", status_code=303)

        # POST/redirect/GET: without the redirect, a refresh re-submits the form.
        return RedirectResponse(url="/console/settings?saved=1", status_code=303)
