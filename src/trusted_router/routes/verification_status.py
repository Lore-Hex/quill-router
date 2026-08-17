from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from trusted_router.auth import ManagementPrincipal, SettingsDep
from trusted_router.errors import api_error
from trusted_router.identity_guidance import guidance_for
from trusted_router.money import (
    VERIFF_ATTEMPT_FEE_MICRODOLLARS,
    VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
    money_pair,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import User
from trusted_router.types import ErrorType
from trusted_router.verification_gates import missing_identity_verification_requirements


def register_verification_status_routes(router: APIRouter) -> None:
    @router.get("/auth/verification-status")
    async def verification_status(
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> dict[str, dict[str, Any]]:
        user = _principal_user(principal)
        lifetime_topup = STORE.get_lifetime_topup_microdollars(user.id)
        guidance = guidance_for(
            user.identity_status,
            reason_code=user.veriff_decision_reason_code,
        )
        return {
            "data": {
                "email": user.email,
                "email_verified": bool(user.email_verified),
                **money_pair("lifetime_topup", lifetime_topup),
                "phone_verified": bool(user.phone_verified),
                "phone": user.phone,
                "identity_status": user.identity_status,
                "identity_verified_at": user.identity_verified_at,
                "veriff_attempt_count": user.veriff_attempt_count,
                # The same copy the console shows. Veriff's own decline reason
                # is never in here: see trusted_router.identity_guidance.
                "identity_message": (
                    None
                    if guidance is None
                    else {"headline": guidance.headline, "detail": guidance.detail}
                ),
                **money_pair("verification_fee", VERIFF_ATTEMPT_FEE_MICRODOLLARS),
                **money_pair(
                    "lifetime_topup_required",
                    VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS,
                ),
                "missing_requirements": missing_identity_verification_requirements(user, settings),
                "next_step": _next_step(user, lifetime_topup),
            }
        }


def _principal_user(principal: Any) -> User:
    """The person this status is ABOUT — and only ever the caller.

    A key minted under another key has no creator, and it must not resolve to
    the workspace owner: that would hand any non-owner admin the owner's phone
    number and email. Same rule as custom_models._owner_user_id — a status
    endpoint that cannot identify a person answers 403, not someone else's PII.
    """
    if principal.user is not None:
        return principal.user
    if principal.api_key is not None and principal.api_key.creator_user_id:
        user = STORE.get_user(principal.api_key.creator_user_id)
        if user is not None:
            return user
    raise api_error(
        403,
        "A user-owned management session or key is required",
        ErrorType.FORBIDDEN,
    )


def _next_step(user: User, lifetime_topup: int) -> str | None:
    if not user.email or not user.email_verified:
        return "email"
    if lifetime_topup < VERIFICATION_MIN_LIFETIME_TOPUP_MICRODOLLARS:
        return "funding"
    if not user.phone_verified:
        return "phone"
    if not user.identity_verified:
        return "identity"
    return None
