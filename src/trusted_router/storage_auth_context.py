"""Backend-neutral assembly for the single-read authentication records."""

from __future__ import annotations

from collections.abc import Iterable

from trusted_router.storage_models import (
    AuthSession,
    Member,
    SessionAuthContext,
    User,
    Workspace,
)


def build_session_auth_context(
    *,
    session: AuthSession,
    user: User | None,
    memberships: Iterable[tuple[Member, Workspace]],
    requested_workspace_id: str | None,
) -> SessionAuthContext:
    """Apply one workspace-selection contract to every storage backend.

    An explicit request or session binding must match exactly.  With neither,
    the first visible membership is the fallback.  Empty-role memberships are
    still memberships for an explicit selection (matching ``user_is_member``)
    but are omitted from the workspace list (matching
    ``list_workspaces_for_user``).  Deleted workspaces are never visible.
    """
    current_memberships = [
        (member, workspace) for member, workspace in memberships if not workspace.deleted
    ]
    visible_workspaces = tuple(
        workspace for member, workspace in current_memberships if member.role
    )
    selected_workspace_id = requested_workspace_id or session.workspace_id
    selected: tuple[Member, Workspace] | None = None
    if selected_workspace_id:
        selected = next(
            (pair for pair in current_memberships if pair[1].id == selected_workspace_id),
            None,
        )
    elif visible_workspaces:
        fallback_id = visible_workspaces[0].id
        selected = next(pair for pair in current_memberships if pair[1].id == fallback_id)
    member = selected[0] if selected is not None else None
    workspace = selected[1] if selected is not None else None
    return SessionAuthContext(
        session=session,
        user=user,
        workspace=workspace,
        workspaces=visible_workspaces,
        is_member=member is not None,
        is_management=(member is not None and member.role in {"owner", "admin"}),
    )
