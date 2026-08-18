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
from trusted_router.verification_gates import missing_phone_verification_requirements

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
        # session_auth_context is one fresh strong read for every request, so
        # ctx already contains the current user and selected membership role.
        user = ctx.user
        phone_missing_requirements = missing_phone_verification_requirements(user, settings)
        _resend_allowed, resend_wait_seconds = pv.can_resend(user)
        return HTMLResponse(
            render(
                "console/settings.html",
                settings=settings,
                ctx=ctx,
                active="settings",
                page_title="Workspace settings",
                page_subtitle="Names, content storage, integrations.",
                can_manage=ctx.can_manage,
                saved=bool(saved),
                error=error,
                phone=user.phone,
                phone_verified=bool(user.phone_verified),
                phone_pending=user.pending_phone,
                phone_code_channel=user.phone_code_channel,
                # Not gated on pending_phone: after "use a different number"
                # the entry form is back but the floor still applies, and the
                # visitor should see the countdown rather than a rate error.
                resend_wait_seconds=resend_wait_seconds,
                notify_sms_available=settings.notify_sms_available,
                phone_sent=sent,
                phone_saved=bool(phone_saved),
                phone_error=error,
                phone_error_detail=detail,
                phone_missing_requirements=phone_missing_requirements,
            )
        )

    @app.post("/console/settings/phone/start")
    async def console_phone_start(
        ctx: ConsoleDep,
        settings: SettingsDep,
        phone: str = Form(""),
        channel: str = Form("voice"),
        sms_consent: str = Form(""),
    ) -> Response:
        """Send a verification code to a number the visitor claims."""
        # Fresh read, not the session snapshot: the floor must see the send
        # that happened seconds ago on this same page, or a fast retry loop
        # (or cancel-then-start) rings the number again before it applies.
        current = STORE.get_user(ctx.user.id) or ctx.user
        missing_requirements = missing_phone_verification_requirements(current, settings)
        if missing_requirements:
            return _back(f"error={missing_requirements[0]}")

        # Consent is a GATE, not a notice: a checkbox enforced only in the
        # browser is decoration, since anyone can post this form without it, and
        # the consent record we would then show a carrier would be a claim with
        # nothing behind it. 10DLC campaign 30909 was rejected for a Call to
        # Action that could not be verified, and the honest reason was that the
        # checkbox described in the submission did not exist at all.
        #
        # Checked AFTER the funding and email requirements, matching the sibling
        # /notify/phone/start route: those are eligibility, and an unfunded
        # account is not even shown this form, so "you cannot do this yet" is
        # the useful answer rather than "tick a box you never saw".
        if sms_consent.strip().lower() not in {"yes", "on", "true", "1"}:
            return _back("error=consent")
        allowed, wait = pv.can_resend(current)
        if not allowed:
            # Without a floor, this form is a way to ring someone else's phone
            # repeatedly by typing their number.
            return _back(f"error=rate&detail={wait}s")

        try:
            normalized = pv.normalize_phone(phone)
        except pv.PhoneNumberError as exc:
            return _back(f"error=phone&detail={quote(str(exc))}")

        wanted: Literal["sms", "voice"] = (
            "sms" if channel == "sms" and settings.notify_sms_available else "voice"
        )
        started = STORE.begin_phone_verification(ctx.user.id, normalized, wanted)
        if started is None:
            return _back("error=phone&detail=account+not+found")
        code, _updated = started

        delivered, detail = send_verification_code(settings, normalized, code, channel=wanted)
        if not delivered:
            # The pending code is left in place deliberately: a carrier blip is
            # not a rejected number, and clearing it would send the visitor
            # back to the start for nothing.
            return _back(f"error=send&detail={quote(detail[:120])}")
        return _back(f"sent={wanted}")

    @app.post("/console/settings/phone/confirm")
    async def console_phone_confirm(ctx: ConsoleDep, code: str = Form("")) -> Response:
        status, _user = STORE.confirm_phone_verification(ctx.user.id, code)
        if status == "ok":
            return _back("phone_saved=1")
        return _back(f"error={status}")

    @app.post("/console/settings/phone/cancel")
    async def console_phone_cancel(ctx: ConsoleDep) -> Response:
        STORE.cancel_phone_verification(ctx.user.id)
        return _back("")

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
