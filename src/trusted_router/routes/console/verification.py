"""Account verification checklist and identity-verification launch."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from trusted_router.auth import SettingsDep
from trusted_router.config import Settings
from trusted_router.identity_guidance import guidance_for
from trusted_router.money import (
    VERIFF_ATTEMPT_FEE_MICRODOLLARS,
    VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
    format_money_display,
)
from trusted_router.routes.console._shared import ConsoleDep, render
from trusted_router.routes.identity_verify import start_identity_session
from trusted_router.storage import STORE
from trusted_router.verification_gates import missing_identity_verification_requirements


def register(app: FastAPI) -> None:
    @app.get("/console/account/verification")
    async def console_verification(
        ctx: ConsoleDep,
        settings: SettingsDep,
        error: str = "",
        veriff: str = "",
        dev: str = "",
    ) -> Response:
        user = STORE.get_user(ctx.user.id) or ctx.user
        # Page progress only; `missing_identity_verification_requirements`
        # below performs the actual gate with the strong default.
        lifetime_topup = STORE.get_lifetime_topup_microdollars(
            user.id,
            allow_stale=True,
        )
        required = VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS
        missing = missing_identity_verification_requirements(user, settings)
        guidance = guidance_for(
            user.identity_status,
            reason_code=user.veriff_decision_reason_code,
        )
        identity_labels = {
            "none": "Not started",
            "pending": "Pending",
            "approved": "Approved",
            "declined": "Declined",
            "resubmission_requested": "Resubmission requested",
            "expired": "Expired",
        }
        return HTMLResponse(
            render(
                "console/account/verification.html",
                settings=settings,
                ctx=ctx,
                active="verification",
                page_title="Account verification",
                page_subtitle="Complete each check once to unlock verified account features.",
                current_user=user,
                error=error,
                veriff_done=veriff == "done",
                dev_approved=bool(dev),
                lifetime_topup=lifetime_topup,
                lifetime_topup_display=format_money_display(lifetime_topup),
                lifetime_topup_required_display=format_money_display(required),
                lifetime_topup_required=required,
                lifetime_progress=min(100, int(lifetime_topup * 100 / required)),
                phone_funding_unlocked=lifetime_topup > 0,
                identity_fee_display=format_money_display(
                    VERIFF_ATTEMPT_FEE_MICRODOLLARS
                ),
                identity_missing_requirements=missing,
                identity_missing_labels=[
                    _REQUIREMENT_LABELS.get(key, key) for key in missing
                ],
                identity_status=user.identity_status,
                identity_status_label=identity_labels.get(
                    user.identity_status,
                    "Not started",
                ),
                identity_action_label=_identity_action_label(user.identity_status),
                identity_guidance=guidance,
                identity_action_enabled=(
                    not missing
                    and not user.identity_verified
                    and _veriff_available(settings)
                ),
                veriff_available=_veriff_available(settings),
            )
        )

    @app.post("/console/account/verification/identity/start")
    async def console_start_identity(
        ctx: ConsoleDep,
        settings: SettingsDep,
    ) -> Response:
        user = STORE.get_user(ctx.user.id) or ctx.user
        try:
            result = start_identity_session(
                user=user,
                workspace_id=ctx.workspace.id,
                settings=settings,
            )
        except HTTPException as exc:
            if exc.status_code == 402:
                error = "insufficient"
            elif exc.status_code == 403:
                error = "prereqs"
            elif exc.status_code == 409:
                return _back("")
            else:
                error = "veriff_unavailable"
            return _back(f"error={error}")
        return RedirectResponse(url=result.url, status_code=303)


#: The API keeps the machine-readable keys; the page shows words. A person
#: reading "phone_verified" is reading our schema, not an instruction.
_REQUIREMENT_LABELS = {
    "email": "a verified email address",
    "phone_verified": "a verified phone number",
    "funding": "the minimum lifetime top-up",
}


def _identity_action_label(status: str) -> str:
    if status in {"declined", "resubmission_requested", "expired"}:
        return "Retry identity verification"
    if status == "pending":
        return "Resume identity verification"
    return "Start identity verification"


def _veriff_available(settings: Settings) -> bool:
    return settings.veriff_enabled or (
        settings.environment.lower() in {"local", "test"}
        and not settings.veriff_configured
    )


def _back(query: str) -> RedirectResponse:
    url = "/console/account/verification"
    if query:
        url += f"?{query}"
    return RedirectResponse(url=url, status_code=303)
