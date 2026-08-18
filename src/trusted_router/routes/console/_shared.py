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

from trusted_router.auth import SESSION_COOKIE_NAME, SettingsDep
from trusted_router.config import Settings
from trusted_router.domains import request_api_base_url
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
    can_manage: bool
    api_base_url: str


def require_console_context(request: Request, settings: SettingsDep) -> ConsoleContext:
    """FastAPI dependency. Resolves the active console session or raises a
    302 redirect to the marketing page so it can pop the sign-in modal."""
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    context = (
        STORE.session_auth_context(cookie_token, requested_workspace_id=None)
        if cookie_token
        else None
    )
    if context is None or context.session.state != "active":
        raise HTTPException(status_code=302, headers={"Location": "/?reason=signin"})
    session = context.session
    user = context.user
    if user is None:
        raise HTTPException(status_code=302, headers={"Location": "/?reason=signin"})
    workspaces = list(context.workspaces)
    if not workspaces:
        raise HTTPException(status_code=302, headers={"Location": "/?reason=signin"})
    workspace = _selected_console_workspace(session, workspaces)
    return ConsoleContext(
        user=user,
        session=session,
        workspace=workspace,
        workspaces=workspaces,
        can_manage=workspace.id in context.management_workspace_ids,
        api_base_url=request_api_base_url(request, settings),
    )


ConsoleDep = Annotated[ConsoleContext, Depends(require_console_context)]


def render(
    template: str,
    *,
    settings: Settings,
    ctx: ConsoleContext,
    **context: Any,
) -> str:
    """Render a console page with one authoritative workspace context.

    Requiring ``ctx`` prevents pages from using the selected workspace for
    their data while accidentally rendering the first workspace in the
    shared selector.
    """
    active = str(context.get("active") or "")
    return render_template(
        template,
        api_base_url=context.pop("api_base_url", ctx.api_base_url),
        user=ctx.user,
        user_email=ctx.user.email,
        workspace=ctx.workspace,
        workspaces=ctx.workspaces,
        current_workspace=ctx.workspace,
        current_workspace_id=ctx.workspace.id,
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
        "custom-models": "/console/custom-models",
        "user-models": "/console/user-models",
        "earnings": "/console/earnings",
        "routing": "/console/routing",
        "activity": "/console/activity",
        "broadcast": "/console/broadcast",
        "settings": "/console/settings",
        "credits": "/console/credits",
        "preferences": "/console/account/preferences",
        "verification": "/console/account/verification",
    }.get(active, "/console/api-keys")


def safe_console_next(next_path: str) -> str:
    if not next_path.startswith("/console/") or next_path.startswith("//"):
        return "/console/api-keys"
    return next_path
