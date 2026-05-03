from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request, Response

from trusted_router.config import Settings
from trusted_router.errors import api_error
from trusted_router.storage import STORE, ApiKey, AuthSession, User, Workspace
from trusted_router.types import ErrorType

SESSION_COOKIE_NAME = "tr_session"
SESSION_COOKIE_MAX_AGE = 86400  # 24h


@dataclass(frozen=True)
class Principal:
    user: User | None
    workspace: Workspace
    api_key: ApiKey | None
    is_management: bool


def bootstrap_management_key(settings: Settings) -> ApiKey | None:
    raw = settings.bootstrap_management_key
    if not raw:
        return None
    existing = STORE.get_key_by_raw(raw)
    if existing is not None:
        return existing
    user = STORE.ensure_user("local-admin", email="local-admin@trustedrouter.local")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    _, key = STORE.create_api_key(
        workspace_id=workspace.id,
        name="Bootstrap management key",
        creator_user_id=user.id,
        raw_key=raw,
        management=True,
    )
    return key


def get_authorization_bearer(request: Request) -> str | None:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    return header.split(" ", 1)[1].strip()


def principal_from_request(request: Request, settings: Settings) -> Principal:
    """Resolve the caller's `Principal` from one of three credential sources.

    Sources are tried in order: dev-user header (local/test only), session
    cookie (browser console), Authorization bearer (API clients). Each source
    is its own helper, all of them route through `_principal_for_session` for
    user-backed flows and `_principal_for_api_key` for the long-lived key
    flow, so the workspace resolution logic isn't duplicated.
    """
    dev_user_id = request.headers.get("x-trustedrouter-user")
    if dev_user_id:
        return _principal_from_dev_header(request, settings, dev_user_id)

    raw_bearer = get_authorization_bearer(request)
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_bearer and cookie_token:
        # Browser console only ever sends the cookie. Resolve as a session;
        # don't fall through to the API-key lookup since we don't have a
        # bearer to look up.
        session = STORE.get_auth_session_by_raw(cookie_token)
        if session is None or session.state != "active":
            raise api_error(401, "Invalid session", "unauthorized")
        return _principal_for_session(request, session)
    if not raw_bearer:
        raise api_error(401, "Missing Authentication header", "unauthorized")

    bootstrap_management_key(settings)
    api_key = STORE.get_key_by_raw(raw_bearer)
    if api_key is not None:
        return _principal_for_api_key(api_key)

    # Bearer didn't match an API key — fall back to session-token shape (used
    # by management programs that prefer a bearer over the cookie).
    session = STORE.get_auth_session_by_raw(raw_bearer)
    if session is not None:
        return _principal_for_session(request, session)
    raise api_error(401, "Invalid API key", "unauthorized")


def _principal_from_dev_header(
    request: Request,
    settings: Settings,
    dev_user_id: str,
) -> Principal:
    if settings.environment.lower() not in {"local", "test"}:
        raise api_error(401, "User header auth is disabled in production", "unauthorized")
    user = STORE.ensure_user(dev_user_id)
    workspace = _resolve_workspace_for_user(
        request,
        user.id,
        suggested_workspace_id=None,
    )
    return Principal(
        user=user,
        workspace=workspace,
        api_key=None,
        is_management=STORE.user_can_manage(user.id, workspace.id),
    )


def _principal_for_session(request: Request, session: AuthSession) -> Principal:
    user = STORE.get_user(session.user_id)
    if user is None:
        raise api_error(401, "Invalid session", "unauthorized")
    workspace = _resolve_workspace_for_user(
        request,
        user.id,
        suggested_workspace_id=session.workspace_id,
    )
    return Principal(
        user=user,
        workspace=workspace,
        api_key=None,
        is_management=STORE.user_can_manage(user.id, workspace.id),
    )


def _principal_for_api_key(api_key: ApiKey) -> Principal:
    if api_key.disabled:
        raise api_error(401, "Invalid API key", "unauthorized")
    if is_api_key_expired(api_key.expires_at):
        raise api_error(401, "API key expired", "unauthorized")
    workspace = STORE.get_workspace(api_key.workspace_id)
    if workspace is None:
        raise api_error(403, "Workspace is unavailable", "forbidden")
    return Principal(
        user=None,
        workspace=workspace,
        api_key=api_key,
        is_management=api_key.management,
    )


def _resolve_workspace_for_user(
    request: Request,
    user_id: str,
    *,
    suggested_workspace_id: str | None,
) -> Workspace:
    """Pick the workspace the caller wants to act on. Header wins, session-
    bound workspace is the fallback, then the user's first membership."""
    workspace_id = request.headers.get("x-trustedrouter-workspace") or suggested_workspace_id
    if workspace_id:
        workspace = STORE.get_workspace(workspace_id)
        if workspace is None or not STORE.user_is_member(user_id, workspace_id):
            raise api_error(403, "Forbidden", "forbidden")
        return workspace
    workspaces = STORE.list_workspaces_for_user(user_id)
    if not workspaces:
        raise api_error(403, "Workspace is unavailable", "forbidden")
    return workspaces[0]


def settings_from_request(request: Request) -> Settings:
    """Pull the per-app `Settings` instance set by `create_app`. Lets auth and
    route handlers depend on `Settings` via FastAPI DI without each one
    needing its own wrapper."""
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(settings_from_request)]


def require_management(
    request: Request,
    settings: SettingsDep,
) -> Principal:
    principal = principal_from_request(request, settings)
    if not principal.is_management:
        raise api_error(
            403,
            "Only management keys can perform this operation",
            ErrorType.FORBIDDEN,
        )
    return principal


def require_inference_key(
    request: Request,
    settings: SettingsDep,
) -> Principal:
    principal = principal_from_request(request, settings)
    if principal.api_key is None:
        raise api_error(403, "An inference API key is required", ErrorType.FORBIDDEN)
    return principal


ManagementPrincipal = Annotated[Principal, Depends(require_management)]
InferencePrincipal = Annotated[Principal, Depends(require_inference_key)]


def is_api_key_expired(expires_at: str | None) -> bool:
    if not expires_at:
        return False
    normalized = expires_at.replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return True
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed <= dt.datetime.now(dt.UTC)


def set_session_cookie(response: Response, raw_token: str, settings: Settings) -> None:
    """Attach the active session cookie to a response. HttpOnly + SameSite=Lax;
    Secure is enabled in production so cookies are never sent over plain HTTP."""
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=raw_token,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        secure=settings.environment.lower() == "production",
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=settings.environment.lower() == "production",
        samesite="lax",
    )
