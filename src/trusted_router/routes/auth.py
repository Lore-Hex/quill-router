from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response

from trusted_router.auth import (
    SESSION_COOKIE_NAME,
    ManagementPrincipal,
    SettingsDep,
    clear_session_cookie,
    get_authorization_bearer,
)
from trusted_router.storage import STORE, User, Workspace


def register_auth_routes(router: APIRouter) -> None:
    @router.get("/auth/session")
    async def auth_session(principal: ManagementPrincipal) -> dict[str, Any]:
        assert principal.user is not None or principal.api_key is not None
        user = principal.user
        return {
            "data": {
                "authenticated": True,
                "workspace": _workspace_payload(principal.workspace),
                "user": _user_payload(user) if user is not None else None,
                "management": principal.is_management,
                "auth_type": "api_key" if principal.api_key else "session",
            }
        }

    @router.post("/auth/logout")
    async def logout(request: Request, settings: SettingsDep) -> Response:
        # Clear from BOTH transports — Bearer header (programmatic) and cookie
        # (browser console). Either may be present; the other is harmless.
        raw = get_authorization_bearer(request) or request.cookies.get(SESSION_COOKIE_NAME)
        deleted = STORE.delete_auth_session_by_raw(raw) if raw else False
        response = Response(
            content=f'{{"data":{{"deleted":{str(deleted).lower()}}}}}',
            media_type="application/json",
        )
        clear_session_cookie(response, settings)
        return response


def _user_payload(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "email": user.email,
        "email_verified": user.email_verified,
        "wallet_address": user.wallet_address,
        "created_at": user.created_at,
    }


def _workspace_payload(workspace: Workspace) -> dict[str, str]:
    return {"id": workspace.id, "name": workspace.name, "created_at": workspace.created_at}
