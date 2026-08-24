"""Backend-neutral laws for the collapsed authentication reads."""

from __future__ import annotations

from trusted_router.store_protocol import Store


def test_session_auth_context_is_complete_and_keeps_state_for_the_auth_gate(
    store: Store,
    user_id: str,
    workspace_id: str,
    unique: str,
) -> None:
    raw_token, session = store.create_auth_session(
        user_id=user_id,
        provider="email",
        label=f"auth-context-{unique}",
        ttl_seconds=3600,
        workspace_id=workspace_id,
        # Storage must return this state.  The security boundary that refuses
        # it remains _principal_for_session, because pending-email flows need
        # to retrieve their session without authenticating it.
        state="pending_email",
    )

    context = store.session_auth_context(
        raw_token,
        requested_workspace_id=None,
    )

    assert context is not None
    assert context.session.hash == session.hash
    assert context.session.state == "pending_email"
    assert context.user is not None and context.user.id == user_id
    assert context.workspace is not None and context.workspace.id == workspace_id
    assert workspace_id in {workspace.id for workspace in context.workspaces}
    assert context.is_member is True
    assert context.is_management is True


def test_api_key_auth_context_returns_workspace_and_rejects_unknown_lookup(
    store: Store,
    user_id: str,
    workspace_id: str,
    unique: str,
) -> None:
    raw_key, api_key = store.create_api_key(
        workspace_id=workspace_id,
        name=f"auth-context-{unique}",
        creator_user_id=user_id,
    )

    context = store.api_key_auth_context(raw_key)

    assert context is not None
    assert context.api_key.hash == api_key.hash
    assert context.workspace is not None
    assert context.workspace.id == workspace_id
    # This is the backend-neutral unknown-lookup law.  The forced-collision
    # tests exercise the distinct, security-critical secret-verification step.
    assert store.api_key_auth_context(f"{raw_key}-tampered") is None
