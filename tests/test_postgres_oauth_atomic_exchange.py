"""Postgres OAuth exchange runs on one real SQL transaction and rolls back."""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

import pytest

from tests.fakes.postgres import (
    SqlitePostgresConn,
    postgres_store_on,
    sqlite_postgres_conn,
)
from trusted_router.storage_models import User, Workspace
from trusted_router.storage_oauth_codes import (
    OAuthCodeMethodMismatch,
    OAuthCodeVerifierMismatch,
    OAuthWorkspaceBillingPaused,
    OAuthWorkspaceUnavailable,
)
from trusted_router.storage_postgres import PostgresStore


def _challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )


def _store(
    *,
    billing_paused: bool = False,
    pkce_method: str | None = "S256",
) -> tuple[PostgresStore, SqlitePostgresConn, str, str, str]:
    conn = sqlite_postgres_conn()
    store = postgres_store_on(conn)
    user = User(id="user-oauth-postgres", email="oauth-postgres@example.com")
    workspace = Workspace(
        id="ws-oauth-postgres",
        name="OAuth Postgres",
        owner_user_id=user.id,
        billing_paused=billing_paused,
    )

    def seed(transaction: Any) -> None:
        store._write_entity_tx(transaction, "user", user.id, user)  # noqa: SLF001
        store._write_entity_tx(  # noqa: SLF001
            transaction,
            "workspace",
            workspace.id,
            workspace,
        )

    store._run_transaction(seed)  # noqa: SLF001
    verifier = "postgres-verifier-" + "p" * 43
    challenge = None
    if pkce_method == "plain":
        challenge = verifier
    elif pkce_method == "S256":
        challenge = _challenge(verifier)
    raw_code, _code = store.create_oauth_authorization_code(
        workspace_id=workspace.id,
        user_id=user.id,
        callback_url="https://postgres-atomic.example.com/cb",
        key_label="Postgres atomic OAuth",
        ttl_seconds=300,
        app_id=30,
        limit_microdollars=9_000_000,
        limit_reset="weekly",
        code_challenge=challenge,
        code_challenge_method=pkce_method,
    )
    return store, conn, raw_code, verifier, workspace.id


def _typed_key_count(conn: SqlitePostgresConn) -> int:
    return int(conn._raw.execute("SELECT count(*) FROM tr_key_limit").fetchone()[0])  # noqa: SLF001


def test_postgres_oauth_partial_key_create_rolls_back_and_can_retry() -> None:
    store, conn, raw_code, verifier, workspace_id = _store()
    conn.fail_on = "INSERT INTO tr_key_limit"

    with pytest.raises(RuntimeError, match="connection reset"):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    assert conn.count_entities("api_key") == 0
    assert conn.count_entities("api_key_lookup") == 0
    assert conn.count_entities("api_key_by_workspace") == 0
    assert _typed_key_count(conn) == 0
    code_body = conn._raw.execute(  # noqa: SLF001
        "SELECT body FROM tr_entities WHERE kind = ?",
        ("oauth_code",),
    ).fetchone()[0]
    assert json.loads(code_body).get("consumed_at") is None

    conn.fail_on = None
    retry = store.exchange_oauth_authorization_code(
        raw_code,
        code_verifier=verifier,
        code_challenge_method="S256",
    )

    assert retry is not None
    assert retry.api_key.workspace_id == workspace_id
    assert retry.api_key.management is False
    assert conn.count_entities("api_key") == 1
    assert conn.count_entities("api_key_lookup") == 1
    assert conn.count_entities("api_key_by_workspace") == 1
    assert _typed_key_count(conn) == 1
    assert store.get_key_by_raw(retry.raw_key) is not None


def test_postgres_success_persists_hashes_only_and_replay_creates_no_key() -> None:
    store, conn, raw_code, verifier, _workspace_id = _store()

    first = store.exchange_oauth_authorization_code(
        raw_code,
        code_verifier=verifier,
        code_challenge_method="S256",
    )
    replay = store.exchange_oauth_authorization_code(
        raw_code,
        code_verifier=verifier,
        code_challenge_method="S256",
    )

    assert first is not None
    assert replay is None
    assert conn.count_entities("api_key") == 1
    durable_bodies = [
        row[0]
        for row in conn._raw.execute(  # noqa: SLF001
            "SELECT body FROM tr_entities"
        ).fetchall()
    ]
    durable_json = json.dumps(durable_bodies)
    assert raw_code not in durable_json
    assert first.raw_key not in durable_json


@pytest.mark.parametrize("method", [None, "plain", "S256"])
def test_postgres_oauth_supports_no_pkce_plain_and_s256(method: str | None) -> None:
    store, _conn, raw_code, verifier, _workspace_id = _store(pkce_method=method)

    result = store.exchange_oauth_authorization_code(
        raw_code,
        code_verifier=verifier if method else None,
        code_challenge_method=method,
    )

    assert result is not None
    assert result.api_key.management is False


def test_postgres_wrong_pkce_proofs_do_not_consume_grant() -> None:
    store, conn, raw_code, verifier, _workspace_id = _store()

    with pytest.raises(OAuthCodeMethodMismatch):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="plain",
        )
    with pytest.raises(OAuthCodeVerifierMismatch):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier="wrong-" + "x" * 43,
            code_challenge_method="S256",
        )

    assert conn.count_entities("api_key") == 0
    assert _typed_key_count(conn) == 0
    assert (
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )
        is not None
    )


def test_postgres_paused_workspace_keeps_grant_retryable() -> None:
    store, conn, raw_code, verifier, workspace_id = _store(billing_paused=True)

    with pytest.raises(OAuthWorkspaceBillingPaused):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    assert conn.count_entities("api_key") == 0
    assert _typed_key_count(conn) == 0

    def resume(transaction: Any) -> None:
        workspace = store._read_entity_tx(  # noqa: SLF001
            transaction,
            "workspace",
            workspace_id,
            Workspace,
            for_update=True,
        )
        assert workspace is not None
        workspace.billing_paused = False
        store._write_entity_tx(  # noqa: SLF001
            transaction,
            "workspace",
            workspace.id,
            workspace,
        )

    store._run_transaction(resume)  # noqa: SLF001
    assert (
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )
        is not None
    )


def test_postgres_missing_workspace_keeps_grant_retryable() -> None:
    store, conn, raw_code, verifier, workspace_id = _store()
    workspace = store._read_entity("workspace", workspace_id, Workspace)  # noqa: SLF001
    assert workspace is not None
    store._run_transaction(  # noqa: SLF001
        lambda transaction: store._delete_entity_tx(  # noqa: SLF001
            transaction,
            "workspace",
            workspace_id,
        )
    )

    with pytest.raises(OAuthWorkspaceUnavailable):
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )

    code_body = conn._raw.execute(  # noqa: SLF001
        "SELECT body FROM tr_entities WHERE kind = ?",
        ("oauth_code",),
    ).fetchone()[0]
    assert json.loads(code_body).get("consumed_at") is None
    assert conn.count_entities("api_key") == 0
    store._run_transaction(  # noqa: SLF001
        lambda transaction: store._write_entity_tx(  # noqa: SLF001
            transaction,
            "workspace",
            workspace_id,
            workspace,
        )
    )
    assert (
        store.exchange_oauth_authorization_code(
            raw_code,
            code_verifier=verifier,
            code_challenge_method="S256",
        )
        is not None
    )
