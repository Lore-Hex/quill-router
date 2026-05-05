"""Shared dependencies for every console page.

`require_console_context` is the FastAPI dependency that gates every
console route on an active session cookie. `_render` fans out the
common template variables (workspaces, current_workspace, navigation
hint) so each per-page handler stays focused on the page's own data.

Splitting these out of the per-page files keeps each page module
short and lets the package's __init__.py wire pages together without
re-importing helpers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request

from trusted_router.auth import SESSION_COOKIE_NAME
from trusted_router.config import Settings
from trusted_router.money import format_money_display
from trusted_router.storage import (
    STORE,
    AuthSession,
    User,
    Workspace,
)
from trusted_router.views import render_template


@dataclass(frozen=True)
class ConsoleContext:
    """Resolved per-request identity for console pages. The session must be
    `state="active"`; pending wallet sessions don't see the console."""

    user: User
    session: AuthSession
    workspace: Workspace
    workspaces: list[Workspace]


def require_console_context(request: Request) -> ConsoleContext:
    """FastAPI dependency. Resolves the active console session or raises a
    302 redirect to the marketing page so it can pop the sign-in modal."""
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    session = STORE.get_auth_session_by_raw(cookie_token) if cookie_token else None
    if session is None or session.state != "active":
        raise HTTPException(status_code=302, headers={"Location": "/?reason=signin"})
    user = STORE.get_user(session.user_id)
    if user is None:
        raise HTTPException(status_code=302, headers={"Location": "/?reason=signin"})
    workspaces = STORE.list_workspaces_for_user(user.id)
    if not workspaces:
        raise HTTPException(status_code=302, headers={"Location": "/?reason=signin"})
    workspace = _selected_console_workspace(session, workspaces)
    return ConsoleContext(user=user, session=session, workspace=workspace, workspaces=workspaces)


ConsoleDep = Annotated[ConsoleContext, Depends(require_console_context)]


def render(template: str, **context: Any) -> str:
    """Common template fan-out: every console template needs the user,
    workspace list, current workspace, and a hint for which sidebar item
    is active. Each page passes its own page_title / page_subtitle / data."""
    settings: Settings = context.pop("settings")
    user: User = context.pop("user")
    active = str(context.get("active") or "")
    workspaces = STORE.list_workspaces_for_user(user.id)
    current_workspace = context.get("workspace")
    if not isinstance(current_workspace, Workspace):
        current_workspace = workspaces[0] if workspaces else None
    return render_template(
        template,
        api_base_url=context.pop("api_base_url", settings.api_base_url),
        user=user,
        user_email=user.email,
        workspaces=workspaces,
        current_workspace=current_workspace,
        current_workspace_id=current_workspace.id if current_workspace else "",
        console_next_path=_console_path_for_active(active),
        **context,
    )


money = format_money_display


def _selected_console_workspace(
    session: AuthSession, workspaces: list[Workspace]
) -> Workspace:
    if session.workspace_id:
        for workspace in workspaces:
            if workspace.id == session.workspace_id:
                return workspace
    return workspaces[0]


def _console_path_for_active(active: str) -> str:
    return {
        "api-keys": "/console/api-keys",
        "byok": "/console/byok",
        "routing": "/console/routing",
        "activity": "/console/activity",
        "broadcast": "/console/broadcast",
        "settings": "/console/settings",
        "credits": "/console/credits",
        "preferences": "/console/account/preferences",
    }.get(active, "/console/api-keys")


def safe_console_next(next_path: str) -> str:
    if not next_path.startswith("/console/") or next_path.startswith("//"):
        return "/console/api-keys"
    return next_path
