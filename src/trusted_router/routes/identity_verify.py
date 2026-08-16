from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from trusted_router.auth import ManagementPrincipal, SettingsDep
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.money import VERIFF_ATTEMPT_FEE_MICRODOLLARS
from trusted_router.services.veriff import (
    VeriffError,
    VeriffSession,
    create_veriff_session,
)
from trusted_router.storage import STORE
from trusted_router.storage_models import User, utcnow
from trusted_router.types import ErrorType
from trusted_router.verification_gates import missing_identity_verification_requirements


def register_identity_verify_routes(router: APIRouter) -> None:
    @router.post("/auth/identity/session")
    async def create_identity_session(
        principal: ManagementPrincipal,
        settings: SettingsDep,
    ) -> JSONResponse:
        user = _principal_user(principal)
        result = start_identity_session(
            user=user,
            workspace_id=principal.workspace.id,
            settings=settings,
        )
        return JSONResponse(
            {"data": {"url": result.url, "status": result.status}},
            status_code=201,
        )


@dataclass(frozen=True)
class IdentitySessionResult:
    url: str
    status: str


def start_identity_session(
    *,
    user: User,
    workspace_id: str,
    settings: Settings,
) -> IdentitySessionResult:
    dev_fallback = _dev_fallback_enabled(settings)
    if not settings.veriff_enabled and not dev_fallback:
        raise api_error(
            503,
            "Identity verification is unavailable",
            ErrorType.SERVICE_UNAVAILABLE,
        )

    missing_requirements = missing_identity_verification_requirements(user, settings)
    if missing_requirements:
        raise api_error(
            403,
            "Identity verification requirements are not met",
            ErrorType.VERIFICATION_REQUIRED,
            extra={
                "missing_requirements": missing_requirements,
                "verification_url": "/console/account/verification",
            },
        )
    if user.identity_verified:
        raise api_error(
            409,
            "Identity is already verified",
            ErrorType.CONFLICT,
        )
    if _reusable_session(user, settings.identity_session_stale_after_days):
        return IdentitySessionResult(
            url=user.veriff_session_url or "",
            status=user.identity_status,
        )

    if dev_fallback:
        session = VeriffSession(
            id=f"dev-{uuid.uuid4()}",
            url="/console/account/verification?dev=1",
        )
    else:
        try:
            session = create_veriff_session(user=user, settings=settings)
        except VeriffError as exc:
            raise api_error(
                502,
                "Identity verification provider is unavailable",
                ErrorType.SERVICE_UNAVAILABLE,
            ) from exc

    event_id = f"veriff_fee:{user.id}:{session.id}"
    debit = STORE.debit_workspace_guarded(
        workspace_id,
        VERIFF_ATTEMPT_FEE_MICRODOLLARS,
        event_id=event_id,
        kind="verification_fee",
    )
    if debit == "insufficient":
        raise api_error(
            402,
            "Insufficient credits for identity verification",
            ErrorType.INSUFFICIENT_CREDITS,
        )

    updated = STORE.set_user_identity_status(
        user.id,
        status="pending",
        session_id=session.id,
        session_url=session.url,
        increment_attempts=True,
    )
    if updated is None:
        raise api_error(404, "User not found", ErrorType.NOT_FOUND)
    if dev_fallback:
        updated = STORE.set_user_identity_status(
            user.id,
            status="approved",
            verified_name="Dev User",
        )
        if updated is None:
            raise api_error(404, "User not found", ErrorType.NOT_FOUND)
    return IdentitySessionResult(url=session.url, status=updated.identity_status)


def _principal_user(principal: Any) -> User:
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


def _dev_fallback_enabled(settings: Settings) -> bool:
    return (
        settings.environment.lower() in {"local", "test"}
        and not settings.veriff_configured
    )


def _reusable_session(user: User, stale_after_days: int) -> bool:
    if user.identity_status not in {"pending", "resubmission_requested"}:
        return False
    if not user.veriff_session_url or not user.veriff_session_created_at:
        return False
    try:
        created = dt.datetime.fromisoformat(
            user.veriff_session_created_at.replace("Z", "+00:00")
        )
    except ValueError:
        return False
    if created.tzinfo is None:
        created = created.replace(tzinfo=dt.UTC)
    return utcnow() - created < dt.timedelta(days=stale_after_days)
