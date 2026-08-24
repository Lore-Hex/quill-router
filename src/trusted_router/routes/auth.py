from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request, Response
from fastapi.responses import RedirectResponse

from trusted_router.auth import (
    SESSION_COOKIE_NAME,
    AuthenticatedPrincipal,
    ManagementPrincipal,
    SettingsDep,
    clear_session_cookie,
    get_authorization_bearer,
)
from trusted_router.storage import STORE, User, Workspace


def register_auth_routes(router: APIRouter) -> None:
    @router.get("/auth/userinfo")
    async def auth_userinfo(principal: AuthenticatedPrincipal) -> dict[str, Any]:
        """OIDC-style userinfo for "Sign in with TrustedRouter". Works with a
        user-scoped (delegated) inference key OR a console session — resolves
        the signed-in user behind the credential and returns their identity.
        A delegated key carries the approving user's id via `creator_user_id`,
        so apps that did the PKCE sign-in can fetch the user's email/profile
        with just the key they received."""
        user = principal.user
        if (
            user is None
            and principal.api_key is not None
            and principal.api_key.creator_user_id
        ):
            user = STORE.get_user(principal.api_key.creator_user_id)
        return {"data": _userinfo_payload(user, principal.workspace)}

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
        accept = request.headers.get("accept", "")
        if "text/html" in accept and "application/json" not in accept:
            redirect_response = RedirectResponse(url="/", status_code=303)
            clear_session_cookie(redirect_response, settings)
            return redirect_response
        json_response = Response(
            content=f'{{"data":{{"deleted":{str(deleted).lower()}}}}}',
            media_type="application/json",
        )
        clear_session_cookie(json_response, settings)
        return json_response


def _userinfo_payload(user: User | None, workspace: Workspace) -> dict[str, Any]:
    """OIDC-shaped userinfo: `sub` is the subject (user id). Wallet-only
    users have no email — `email` is null and `wallet_address` is set."""
    if user is None:
        return {"sub": None, "workspace_id": workspace.id}
    return {
        "sub": user.id,
        "email": user.email,
        "email_verified": user.email_verified,
        "wallet_address": user.wallet_address,
        "workspace_id": workspace.id,
        "created_at": user.created_at,
    }


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
