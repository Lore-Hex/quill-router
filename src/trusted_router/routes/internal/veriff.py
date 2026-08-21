from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Request

from trusted_router.auth import SettingsDep
from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.routes.internal.webhook_work import run_provider_webhook_work
from trusted_router.storage import STORE
from trusted_router.types import ErrorType
from trusted_router.veriff_verify import (
    VeriffVerificationError,
    verify_veriff_signature,
)

log = logging.getLogger(__name__)


def register(router: APIRouter) -> None:
    @router.post("/internal/veriff/webhook")
    async def veriff_webhook(request: Request, settings: SettingsDep) -> dict[str, Any]:
        raw = await request.body()
        return await run_provider_webhook_work(
            "veriff",
            _process_veriff_webhook,
            raw,
            request.headers.get("x-hmac-signature"),
            settings,
        )


def _process_veriff_webhook(
    raw: bytes,
    signature: str | None,
    settings: Settings,
) -> dict[str, Any]:
    if settings.veriff_shared_secret_key:
        try:
            verify_veriff_signature(
                raw,
                signature,
                shared_secret=settings.veriff_shared_secret_key,
            )
        except VeriffVerificationError as exc:
            raise api_error(
                403,
                "Veriff signature verification failed",
                ErrorType.FORBIDDEN,
            ) from exc
    elif settings.environment.lower() not in {"local", "test"}:
        raise api_error(
            403,
            "Veriff webhook is not configured",
            ErrorType.FORBIDDEN,
        )

    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"data": {"ignored": True}}
    verification = payload.get("verification") if isinstance(payload, dict) else None
    if not isinstance(verification, dict):
        return {"data": {"ignored": True}}

    session_id = verification.get("id")
    provider_status = verification.get("status")
    vendor_data = verification.get("vendorData")
    raw_code = verification.get("code")
    if not isinstance(raw_code, (int, str)):
        return {"data": {"ignored": True}}
    try:
        code = int(raw_code)
    except (TypeError, ValueError):
        return {"data": {"ignored": True}}
    decision_status = mapped_status(provider_status, code)
    if (
        not isinstance(session_id, str)
        or not session_id
        or not isinstance(vendor_data, str)
        or not vendor_data
        or decision_status is None
    ):
        return {"data": {"ignored": True}}

    event_id = f"{session_id}#{provider_status}#{code}"
    if not STORE.record_webhook_event_once("veriff", event_id):
        return {"data": {"replayed": True}}

    user = STORE.get_user(vendor_data)
    if user is None:
        log.info("veriff_webhook.ignored reason=unknown_user")
        return {"data": {"ignored": True}}
    if session_id != user.veriff_session_id:
        log.info("veriff_webhook.ignored reason=stale_session")
        return {"data": {"ignored": True}}

    approved_name = verified_name(verification) if decision_status == "approved" else None
    reason, reason_code = decision_reason(verification)
    STORE.set_user_identity_status(
        user.id,
        status=decision_status,
        decision_code=code,
        decision_reason=reason,
        decision_reason_code=reason_code,
        verified_name=approved_name,
    )
    log.info(
        "veriff_webhook.decision status=%s code=%s reason_code=%s",
        decision_status,
        code,
        reason_code,
    )
    return {"data": {"status": decision_status}}


def mapped_status(provider_status: Any, code: int) -> str | None:
    status = str(provider_status or "").strip().lower()
    if status == "approved" and code == 9001:
        return "approved"
    if status == "declined" and code == 9102:
        return "declined"
    if status == "resubmission_requested" and code == 9103:
        return "resubmission_requested"
    if status in {"expired", "abandoned"} and code == 9104:
        return "expired"
    return None


#: Veriff's `reason` is free text they author, so it is bounded before storage
#: and never rendered to the person being verified — only to operators.
MAX_REASON_LENGTH = 200


def decision_reason(verification: dict[str, Any]) -> tuple[str | None, int | None]:
    """Pull Veriff's granular reason off a decision webhook.

    Kept for operators. Whether any of it reaches the end user is decided by
    :mod:`trusted_router.identity_guidance`, not here.
    """
    raw_reason = verification.get("reason")
    reason = str(raw_reason).strip()[:MAX_REASON_LENGTH] if isinstance(raw_reason, str) else None
    raw_code = verification.get("reasonCode")
    reason_code: int | None
    if isinstance(raw_code, bool):
        reason_code = None
    elif isinstance(raw_code, (int, str)):
        try:
            reason_code = int(raw_code)
        except (TypeError, ValueError):
            reason_code = None
    else:
        reason_code = None
    return (reason or None), reason_code


def verified_name(verification: dict[str, Any]) -> str | None:
    person = verification.get("person")
    if not isinstance(person, dict):
        return None
    parts = [
        str(person.get(field) or "").strip()
        for field in ("firstName", "lastName")
    ]
    name = " ".join(part for part in parts if part)
    return name or None
