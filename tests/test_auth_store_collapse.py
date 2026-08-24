from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException, Request

from tests.fakes.spanner import make_fake_store
from trusted_router.auth import principal_from_request
from trusted_router.config import Settings
from trusted_router.routes.console._shared import require_console_context
from trusted_router.storage import InMemoryStore, configure_store
from trusted_router.storage_codec import json_body
from trusted_router.storage_models import Member
from trusted_router.storage_postgres import PostgresStore


def test_session_auth_context_is_one_store_operation() -> None:
    store = InMemoryStore()
    user = store.ensure_user("fanout-session@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, _session = store.create_auth_session(
        user_id=user.id,
        provider="email",
        label="fanout-session@example.com",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )

    context = store.session_auth_context(raw, requested_workspace_id=None)

    assert context is not None
    assert context.session.user_id == user.id
    assert context.user == user
    assert context.workspace == workspace
    assert context.workspaces == (workspace,)
    assert context.is_member is True
    assert context.is_management is True


def test_api_key_auth_context_resolves_key_and_workspace_together() -> None:
    store = InMemoryStore()
    user = store.ensure_user("fanout-key@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, api_key = store.create_api_key(
        workspace_id=workspace.id,
        name="fanout",
        creator_user_id=user.id,
    )

    context = store.api_key_auth_context(raw)

    assert context is not None
    assert context.api_key == api_key
    assert context.workspace == workspace


def test_session_principal_does_not_fall_back_to_legacy_store_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user("collapsed-session@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, _session = store.create_auth_session(
        user_id=user.id,
        provider="email",
        label="collapsed-session@example.com",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    _reject_legacy_auth_reads(monkeypatch)

    principal = principal_from_request(
        _request(cookie=raw),
        Settings(environment="test"),
    )

    assert principal.user == user
    assert principal.workspace == workspace
    assert principal.is_management is True


def test_api_key_principal_does_not_fall_back_to_legacy_store_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user("collapsed-key@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, api_key = store.create_api_key(
        workspace_id=workspace.id,
        name="collapsed key",
        creator_user_id=user.id,
        management=True,
    )
    _reject_legacy_auth_reads(monkeypatch)

    principal = principal_from_request(
        _request(authorization=f"Bearer {raw}"),
        Settings(environment="test"),
    )

    assert principal.api_key == api_key
    assert principal.workspace == workspace
    assert principal.is_management is True


def test_console_context_uses_collapsed_read_and_keeps_stale_selection_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user("console-collapse@example.com")
    first_workspace = store.list_workspaces_for_user(user.id)[0]
    second_workspace = store.create_workspace(user.id, "Second")
    raw, _session = store.create_auth_session(
        user_id=user.id,
        provider="email",
        label="console-collapse@example.com",
        ttl_seconds=3600,
        # Console treats this binding as a preference and falls back to the
        # first current workspace when the saved selection is stale.
        workspace_id="workspace-that-no-longer-exists",
    )
    _reject_legacy_auth_reads(monkeypatch)

    context = require_console_context(
        _request(cookie=raw),
        Settings(environment="test"),
    )

    assert context.user == user
    assert context.workspace == first_workspace
    assert context.workspaces == [first_workspace, second_workspace]


@pytest.mark.parametrize("state", ["pending_email", "revoked"])
def test_console_collapsed_read_preserves_inactive_session_redirect(
    state: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user(f"console-{state}@example.com")
    raw, _session = store.create_auth_session(
        user_id=user.id,
        provider="email",
        label=user.email or user.id,
        ttl_seconds=3600,
        state=state,
    )
    _reject_legacy_auth_reads(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        require_console_context(
            _request(cookie=raw),
            Settings(environment="test"),
        )

    assert exc_info.value.status_code == 302
    assert exc_info.value.headers == {"Location": "/?reason=signin"}


def test_membership_removal_is_visible_on_the_next_session_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user("removed-member@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, _session = store.create_auth_session(
        user_id=user.id,
        provider="email",
        label="removed-member@example.com",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    request = _request(cookie=raw)
    settings = Settings(environment="test")
    _reject_legacy_auth_reads(monkeypatch)
    assert principal_from_request(request, settings).workspace == workspace

    store.remove_members(workspace.id, [user.id])

    with pytest.raises(HTTPException) as exc_info:
        principal_from_request(request, settings)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"]["message"] == "Forbidden"


def test_session_revocation_is_visible_on_the_next_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user("revoked-session@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, _session = store.create_auth_session(
        user_id=user.id,
        provider="email",
        label="revoked-session@example.com",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    request = _request(cookie=raw)
    settings = Settings(environment="test")
    _reject_legacy_auth_reads(monkeypatch)
    assert principal_from_request(request, settings).workspace == workspace

    assert store.delete_auth_session_by_raw(raw) is True

    with pytest.raises(HTTPException) as exc_info:
        principal_from_request(request, settings)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"]["message"] == "Invalid session"


def test_unbound_session_without_memberships_keeps_workspace_unavailable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user("no-workspace@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, _session = store.create_auth_session(
        user_id=user.id,
        provider="email",
        label="no-workspace@example.com",
        ttl_seconds=3600,
        workspace_id=None,
    )
    store.remove_members(workspace.id, [user.id])
    _reject_legacy_auth_reads(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        principal_from_request(
            _request(cookie=raw),
            Settings(environment="test"),
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["error"]["message"] == "Workspace is unavailable"


def test_key_revocation_is_visible_on_the_next_api_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryStore()
    configure_store(store)
    user = store.ensure_user("revoked-key@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, api_key = store.create_api_key(
        workspace_id=workspace.id,
        name="revocable",
        creator_user_id=user.id,
    )
    request = _request(authorization=f"Bearer {raw}")
    settings = Settings(environment="test")
    _reject_legacy_auth_reads(monkeypatch)
    assert principal_from_request(request, settings).api_key == api_key

    assert store.delete_key(api_key.hash) is True

    with pytest.raises(HTTPException) as exc_info:
        principal_from_request(request, settings)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail["error"]["message"] == "Invalid API key"


def test_spanner_session_context_uses_one_read_rpc() -> None:
    store, database, _bigtable = make_fake_store()
    user = store.ensure_user("spanner-session-fanout@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, _session = store.create_auth_session(
        user_id=user.id,
        provider="email",
        label="spanner-session-fanout@example.com",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    before = database.snapshot_execute_sql_calls
    snapshot_before = len(database.snapshot_calls)

    context = store.session_auth_context(raw, requested_workspace_id=None)

    assert context is not None and context.workspace == workspace
    assert database.snapshot_execute_sql_calls - before == 1
    assert database.snapshot_calls[snapshot_before:] == [{}]
    assert "/* auth_session_context */" in database.snapshot_sql[-1]


def test_spanner_api_key_context_uses_one_read_rpc() -> None:
    store, database, _bigtable = make_fake_store()
    user = store.ensure_user("spanner-key-fanout@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    raw, api_key = store.create_api_key(
        workspace_id=workspace.id,
        name="spanner fanout",
        creator_user_id=user.id,
    )
    before = database.snapshot_execute_sql_calls
    snapshot_before = len(database.snapshot_calls)

    context = store.api_key_auth_context(raw)

    assert context is not None and context.api_key == api_key
    assert context.workspace == workspace
    assert database.snapshot_execute_sql_calls - before == 1
    assert database.snapshot_calls[snapshot_before:] == [{}]
    assert "/* api_key_auth_context */" in database.snapshot_sql[-1]


def test_spanner_session_context_excludes_noncanonical_member_row() -> None:
    store, _database, _bigtable = make_fake_store()
    user = store.ensure_user("spanner-rogue-member@example.com")
    fallback_workspace = store.list_workspaces_for_user(user.id)[0]
    other_user = store.ensure_user("spanner-rogue-target@example.com")
    target_workspace = store.list_workspaces_for_user(other_user.id)[0]
    raw, _session = store.create_auth_session(
        user_id=user.id,
        provider="email",
        label="spanner-rogue-member@example.com",
        ttl_seconds=3600,
        workspace_id=None,
    )
    rogue_member = Member(
        workspace_id=target_workspace.id,
        user_id=user.id,
        role="owner",
    )
    # The body claims membership, but both legacy accessors reject this row:
    # its entity id is neither the exact workspace#user key nor a #user suffix.
    store._write_entity("member", "rogue-noncanonical-member-row", rogue_member)
    assert store.user_is_member(user.id, target_workspace.id) is False
    assert target_workspace not in store.list_workspaces_for_user(user.id)

    requested = store.session_auth_context(
        raw,
        requested_workspace_id=target_workspace.id,
    )
    fallback = store.session_auth_context(raw, requested_workspace_id=None)

    assert requested is not None
    assert requested.workspace is None
    assert requested.is_member is False
    assert requested.is_management is False
    assert target_workspace not in requested.workspaces
    assert fallback is not None
    assert fallback.workspace == fallback_workspace
    assert fallback.workspaces == (fallback_workspace,)


def test_postgres_session_context_excludes_noncanonical_member_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = InMemoryStore()
    user = memory.ensure_user("postgres-rogue-member@example.com")
    fallback_workspace = memory.list_workspaces_for_user(user.id)[0]
    fallback_member = memory.members[(fallback_workspace.id, user.id)]
    other_user = memory.ensure_user("postgres-rogue-target@example.com")
    target_workspace = memory.list_workspaces_for_user(other_user.id)[0]
    raw_session, session = memory.create_auth_session(
        user_id=user.id,
        provider="email",
        label="postgres-rogue-member@example.com",
        ttl_seconds=3600,
        workspace_id=None,
    )
    _raw_key, api_key = memory.create_api_key(
        workspace_id=fallback_workspace.id,
        name="postgres rogue member",
        creator_user_id=user.id,
    )
    rogue_member = Member(
        workspace_id=target_workspace.id,
        user_id=user.id,
        role="owner",
    )
    connection = _FakePostgresConnection(
        session_rows=[
            (
                json_body(session),
                json_body(user),
                json_body(fallback_workspace),
                json_body(fallback_member),
            ),
            (
                json_body(session),
                json_body(user),
                json_body(target_workspace),
                json_body(rogue_member),
            ),
        ],
        session_member_ids=[
            f"{fallback_workspace.id}#{user.id}",
            "rogue-noncanonical-member-row",
        ],
        api_key_row=(json_body(api_key), json_body(fallback_workspace)),
        session_lookup_hash=session.lookup_hash,
        api_key_lookup_hash=api_key.lookup_hash,
    )
    monkeypatch.setattr(
        PostgresStore,
        "_run_transaction",
        lambda _self, operation: operation(connection),
    )
    store = PostgresStore.__new__(PostgresStore)

    requested = store.session_auth_context(
        raw_session,
        requested_workspace_id=target_workspace.id,
    )
    fallback = store.session_auth_context(
        raw_session,
        requested_workspace_id=None,
    )

    assert requested is not None
    assert requested.workspace is None
    assert requested.is_member is False
    assert requested.is_management is False
    assert target_workspace not in requested.workspaces
    assert fallback is not None
    assert fallback.workspace == fallback_workspace
    assert fallback.workspaces == (fallback_workspace,)


@pytest.mark.parametrize("backend", ["spanner", "postgres"])
def test_session_auth_context_verifies_secret_after_forced_lookup_collision(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, session_lookup_hash, _api_key_lookup_hash = _collision_store(
        backend,
        monkeypatch,
    )
    _force_lookup_hash(monkeypatch, backend, session_lookup_hash)

    context = store.session_auth_context(
        "trsess-v1-forced-lookup-collision",
        requested_workspace_id=None,
    )

    # The lookup intentionally resolves somebody else's valid row.  Only the
    # salted secret verification prevents that row from authenticating.
    assert context is None


@pytest.mark.parametrize("backend", ["spanner", "postgres"])
def test_api_key_auth_context_verifies_secret_after_forced_lookup_collision(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _session_lookup_hash, api_key_lookup_hash = _collision_store(
        backend,
        monkeypatch,
    )
    _force_lookup_hash(monkeypatch, backend, api_key_lookup_hash)

    context = store.api_key_auth_context("sk-tr-v1-forced-lookup-collision")

    # A lookup hit is not authentication: the supplied secret must still
    # verify against the returned key before the context can be trusted.
    assert context is None


def test_postgres_auth_contexts_each_issue_one_statement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory = InMemoryStore()
    user = memory.ensure_user("postgres-fanout@example.com")
    workspace = memory.list_workspaces_for_user(user.id)[0]
    member = memory.members[(workspace.id, user.id)]
    raw_session, session = memory.create_auth_session(
        user_id=user.id,
        provider="email",
        label="postgres-fanout@example.com",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    raw_key, api_key = memory.create_api_key(
        workspace_id=workspace.id,
        name="postgres fanout",
        creator_user_id=user.id,
    )
    connection = _FakePostgresConnection(
        session_rows=[
            (
                json_body(session),
                json_body(user),
                json_body(workspace),
                json_body(member),
            )
        ],
        session_member_ids=[f"{workspace.id}#{user.id}"],
        api_key_row=(json_body(api_key), json_body(workspace)),
        session_lookup_hash=session.lookup_hash,
        api_key_lookup_hash=api_key.lookup_hash,
    )
    monkeypatch.setattr(
        PostgresStore,
        "_run_transaction",
        lambda _self, operation: operation(connection),
    )
    store = PostgresStore.__new__(PostgresStore)

    session_context = store.session_auth_context(
        raw_session,
        requested_workspace_id=None,
    )
    assert session_context is not None and session_context.workspace == workspace
    assert len(connection.calls) == 1
    assert "/* auth_session_context */" in connection.calls[-1][0]

    key_context = store.api_key_auth_context(raw_key)
    assert key_context is not None and key_context.api_key == api_key
    assert key_context.workspace == workspace
    assert len(connection.calls) == 2
    assert "/* api_key_auth_context */" in connection.calls[-1][0]


def _collision_store(
    backend: str,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Any, str, str]:
    if backend == "spanner":
        store, _database, _bigtable = make_fake_store()
        user = store.ensure_user("spanner-collision@example.com")
        workspace = store.list_workspaces_for_user(user.id)[0]
        _raw_session, session = store.create_auth_session(
            user_id=user.id,
            provider="email",
            label="spanner-collision@example.com",
            ttl_seconds=3600,
            workspace_id=workspace.id,
        )
        _raw_key, api_key = store.create_api_key(
            workspace_id=workspace.id,
            name="spanner collision",
            creator_user_id=user.id,
        )
        return store, session.lookup_hash, api_key.lookup_hash

    memory = InMemoryStore()
    user = memory.ensure_user("postgres-collision@example.com")
    workspace = memory.list_workspaces_for_user(user.id)[0]
    member = memory.members[(workspace.id, user.id)]
    _raw_session, session = memory.create_auth_session(
        user_id=user.id,
        provider="email",
        label="postgres-collision@example.com",
        ttl_seconds=3600,
        workspace_id=workspace.id,
    )
    _raw_key, api_key = memory.create_api_key(
        workspace_id=workspace.id,
        name="postgres collision",
        creator_user_id=user.id,
    )
    connection = _FakePostgresConnection(
        session_rows=[
            (
                json_body(session),
                json_body(user),
                json_body(workspace),
                json_body(member),
            )
        ],
        session_member_ids=[f"{workspace.id}#{user.id}"],
        api_key_row=(json_body(api_key), json_body(workspace)),
        session_lookup_hash=session.lookup_hash,
        api_key_lookup_hash=api_key.lookup_hash,
    )
    monkeypatch.setattr(
        PostgresStore,
        "_run_transaction",
        lambda _self, operation: operation(connection),
    )
    return PostgresStore.__new__(PostgresStore), session.lookup_hash, api_key.lookup_hash


def _force_lookup_hash(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    lookup_hash: str,
) -> None:
    module = (
        "trusted_router.storage_gcp"
        if backend == "spanner"
        else "trusted_router.storage_postgres"
    )
    monkeypatch.setattr(f"{module}.lookup_hash_api_key", lambda _raw: lookup_hash)


def _request(
    *,
    cookie: str | None = None,
    authorization: str | None = None,
) -> Request:
    headers = [(b"host", b"trustedrouter.com")]
    if cookie is not None:
        headers.append((b"cookie", f"tr_session={cookie}".encode()))
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("trustedrouter.com", 443),
            "app": SimpleNamespace(state=SimpleNamespace()),
        }
    )


def _reject_legacy_auth_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    def unexpected(_self: InMemoryStore, *_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("auth fell back to a legacy Store round trip")

    for method_name in (
        "get_auth_session_by_raw",
        "get_key_by_raw",
        "get_user",
        "get_workspace",
        "list_workspaces_for_user",
        "user_can_manage",
        "user_is_member",
    ):
        monkeypatch.setattr(InMemoryStore, method_name, unexpected)


class _FakePostgresCursor:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows

    def fetchone(self) -> tuple[Any, ...] | None:
        return self._rows[0] if self._rows else None


class _FakePostgresConnection:
    def __init__(
        self,
        *,
        session_rows: list[tuple[Any, ...]],
        session_member_ids: list[str | None],
        api_key_row: tuple[Any, ...],
        session_lookup_hash: str,
        api_key_lookup_hash: str,
    ) -> None:
        self._session_rows = session_rows
        self._session_member_ids = session_member_ids
        self._api_key_row = api_key_row
        self._session_lookup_hash = session_lookup_hash
        self._api_key_lookup_hash = api_key_lookup_hash
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        if len(self._session_rows) != len(self._session_member_ids):
            raise ValueError("each fake Postgres session row needs its member entity id")

    def execute(
        self,
        sql: str,
        params: tuple[Any, ...],
    ) -> _FakePostgresCursor:
        self.calls.append((sql, params))
        if "/* auth_session_context */" in sql:
            assert params == (self._session_lookup_hash,)
            assert "lookup_record.id = %s" in sql
            assert "session_record.id = lookup_record.body ->> 'session_id'" in sql
            assert "user_record.id = resolved.user_id" in sql
            assert "member_record.body ->> 'user_id' = resolved.user_id" in sql
            assert (
                "member_record.id = ((member_record.body ->> 'workspace_id') || '#' || resolved.user_id)"
                in sql
            )
            assert "workspace_record.id = member_record.body ->> 'workspace_id'" in sql
            rows = self._canonical_session_rows()
        else:
            assert params == (self._api_key_lookup_hash,)
            assert "/* api_key_auth_context */" in sql
            assert "lookup_record.id = %s" in sql
            assert "key_record.id = lookup_record.body ->> 'key_id'" in sql
            assert "workspace_record.id = key_record.body ->> 'workspace_id'" in sql
            rows = [self._api_key_row]
        return _FakePostgresCursor(rows)

    def _canonical_session_rows(self) -> list[tuple[Any, ...]]:
        rows: list[tuple[Any, ...]] = []
        for member_id, row in zip(
            self._session_member_ids,
            self._session_rows,
            strict=True,
        ):
            session_body, _user_body, _workspace_body, member_body = row
            if member_body is None:
                rows.append(row)
                continue
            session = _json_record(session_body)
            member = _json_record(member_body)
            user_id = str(session["user_id"])
            if member.get("user_id") != user_id:
                continue
            if member_id != f"{member.get('workspace_id')}#{user_id}":
                continue
            rows.append(row)
        if rows or not self._session_rows:
            return rows
        session_body, user_body, _workspace_body, _member_body = self._session_rows[0]
        return [(session_body, user_body, None, None)]


def _json_record(raw: Any) -> dict[str, Any]:
    return json.loads(raw) if isinstance(raw, str) else dict(raw)
