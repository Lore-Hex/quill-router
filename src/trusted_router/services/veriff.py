from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from trusted_router.config import Settings
from trusted_router.services.telephony import control_plane_public_origin
from trusted_router.storage_models import User


class VeriffError(RuntimeError):
    """Veriff could not create a usable verification session."""


@dataclass(frozen=True)
class VeriffSession:
    id: str
    url: str


def create_veriff_session(*, user: User, settings: Settings) -> VeriffSession:
    if not settings.veriff_api_key:
        raise VeriffError("Veriff API key is not configured")
    callback = (
        control_plane_public_origin(settings)
        + "/console/account/verification?veriff=done"
    )
    try:
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                settings.veriff_base_url.rstrip("/") + "/v1/sessions",
                headers={"X-AUTH-CLIENT": settings.veriff_api_key},
                json={
                    "verification": {
                        "vendorData": user.id,
                        "callback": callback,
                    }
                },
            )
        response.raise_for_status()
        payload: Any = response.json()
        verification = payload.get("verification") if isinstance(payload, dict) else None
        session_id = verification.get("id") if isinstance(verification, dict) else None
        session_url = verification.get("url") if isinstance(verification, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise VeriffError("Veriff response did not include a session id")
        if not isinstance(session_url, str) or not session_url:
            raise VeriffError("Veriff response did not include a session URL")
        return VeriffSession(id=session_id, url=session_url)
    except (httpx.HTTPError, ValueError) as exc:
        raise VeriffError("Veriff session creation failed") from exc
