"""Run the real Postgres claim SQL and rollback path without network calls."""

import json

import pytest

from tests.fakes.postgres import postgres_store_on, sqlite_postgres_conn
from trusted_router.storage_models import Member

NOW = "2026-09-01T12:00:00Z"


def test_postgres_claims_persist_across_store_instances_and_do_not_expire():
    conn = sqlite_postgres_conn()
    try:
        store = postgres_store_on(conn)
        assert store.claim_retirement_notices("ws", ["b", "a", "a"], occurred_at=NOW) == ["a", "b"]
        restarted = postgres_store_on(conn)
        assert restarted.claim_retirement_notices("ws", ["a", "b"], occurred_at="2030-01-01T00:00:00Z") == []
        assert restarted.claim_retirement_notices("ws", ["b", "c"], occurred_at=NOW) == ["c"]
        assert restarted.claim_retirement_notices("ws-other", ["a", "b"], occurred_at=NOW) == ["a", "b"]
        body = conn.execute("SELECT body FROM tr_entities WHERE kind = %s AND id = %s", ("retirement_notice", "ws")).fetchone()[0]
        assert json.loads(body) == {"a": NOW, "b": NOW, "c": NOW}
        assert any("FOR UPDATE" in sql for sql, _ in conn.statements)
    finally:
        conn._raw.close()


def test_failed_postgres_claim_rolls_back_before_retry():
    conn = sqlite_postgres_conn()
    try:
        store = postgres_store_on(conn)
        conn.fail_on = "DO UPDATE"
        with pytest.raises(RuntimeError, match="connection reset"):
            store.claim_retirement_notices("ws", ["a", "b"], occurred_at=NOW)
        assert conn.count_entities("retirement_notice") == 0
        conn.fail_on = None
        assert store.claim_retirement_notices("ws", ["a", "b"], occurred_at=NOW) == ["a", "b"]
    finally:
        conn._raw.close()


def test_postgres_recipient_members_and_blocks_use_bounded_keys():
    conn = sqlite_postgres_conn()
    try:
        store = postgres_store_on(conn)
        for workspace, user, role in [
            ("ws_1", "owner", "owner"), ("ws_1", "admin", "admin"),
            ("ws_1", "member", "member"), ("wsX1", "other", "owner"),
            ("ws_10", "other", "admin"),
        ]:
            store._run_transaction(lambda connection, workspace=workspace, user=user, role=role: store._write_entity_tx(
                connection, "member", f"{workspace}#{user}", Member(workspace, user, role),
            ))
        assert [(member.user_id, member.role) for member in store.list_members("ws_1")] == [
            ("admin", "admin"), ("member", "member"), ("owner", "owner"),
        ]
        assert store.list_members("missing") == []
        store.block_email_sending(email=" BLOCKED@EXAMPLE.COM ", reason="bounce", mail_class="retirement_notice")
        restarted = postgres_store_on(conn)
        assert restarted.is_email_blocked("blocked@example.com") is True
        assert restarted.is_email_blocked("unblocked@example.com") is False
        block = restarted.get_email_block("BLOCKED@example.com")
        assert (block.reason, block.mail_class) == ("bounce", "retirement_notice")
        # An escaped LIKE prefix: collation-independent, and "_" in ws_1 is literal
        # (wsX1 and ws_10 above are excluded).
        assert any("id LIKE %s ESCAPE" in sql for sql, _ in conn.statements)
    finally:
        conn._raw.close()
