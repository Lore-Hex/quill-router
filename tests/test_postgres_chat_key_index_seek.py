"""The Postgres chat-key index read is an exact primary-key range seek under
both a locale database collation and the C collation.

A LIKE prefix seeks only under C (Aurora DSQL); under a locale collation
(Azure Flexible Server's default en_US) it scans the kind. A range on a
<workspace>#<user># prefix seeks but is wrong there, because the locale
collation ignores '#'. The index id starts with a fixed-width hex digest
instead, so the same range is exact and seeks under both.

Runs against TR_CONFORMANCE_POSTGRES_DSN; needs CREATEDB to make the C
database. Skipped when no server is configured.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from typing import Any

import pytest

from trusted_router.storage_key_usage import (
    CHAT_BROWSER_KEY_INDEX_KIND,
    chat_browser_key_index_id,
    chat_browser_key_index_prefix,
    chat_browser_key_index_upper,
)
from trusted_router.storage_postgres import _CHAT_BROWSER_KEY_INDEX_SQL, PostgresStore

DSN = os.environ.get("TR_CONFORMANCE_POSTGRES_DSN")
needs_server = pytest.mark.skipif(not DSN, reason="set TR_CONFORMANCE_POSTGRES_DSN")
C_DATABASE = "tr_chat_index_seek_c"


def _with_database(dsn: str, database: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    params = conninfo_to_dict(dsn)
    params["dbname"] = database
    return make_conninfo(**params)


@pytest.fixture(scope="module")
def databases() -> Iterator[dict[str, str]]:
    import psycopg

    assert DSN is not None
    with psycopg.connect(DSN, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (C_DATABASE,)).fetchone()
        if not exists:
            conn.execute(
                f"CREATE DATABASE {C_DATABASE} TEMPLATE template0 LC_COLLATE 'C' LC_CTYPE 'C'"
            )
        default_collation = conn.execute(
            "SELECT datcollate FROM pg_database WHERE datname = current_database()"
        ).fetchone()[0]
    yield {"default": DSN, "c": _with_database(DSN, C_DATABASE), "default_collation": default_collation}


def _seeded(conn: Any) -> list[tuple[str, str]]:
    """A temp tr_entities shadowing the real one: 1,200 users' index rows
    (five keys each, ids with '_' and mixed case after the digest) and
    unrelated rows of other kinds. Returns the (workspace, user) pairs."""
    conn.execute(
        "CREATE TEMP TABLE tr_entities (kind TEXT NOT NULL, id TEXT NOT NULL, "
        "body JSONB NOT NULL, PRIMARY KEY (kind, id))"
    )
    pairs = [(f"ws_{n % 300}", f"user_{n}") for n in range(1_200)]
    rows = [
        (CHAT_BROWSER_KEY_INDEX_KIND, chat_browser_key_index_id(ws, user, f"key_{ws}_{user}_{i}_AbC"),
         json.dumps({"key_id": f"key_{ws}_{user}_{i}_AbC"}))
        for ws, user in pairs for i in range(5)
    ]
    rows += [("api_key", f"key_other_{n}", "{}") for n in range(6_000)]
    with conn.cursor() as cursor:
        cursor.executemany("INSERT INTO tr_entities (kind, id, body) VALUES (%s, %s, %s)", rows)
    conn.execute("ANALYZE tr_entities")
    return pairs


@needs_server
@pytest.mark.parametrize("which", ["default", "c"])
def test_index_read_is_an_exact_primary_key_seek(databases: dict[str, str], which: str) -> None:
    import psycopg

    with psycopg.connect(databases[which]) as conn:
        pairs = _seeded(conn)
        # Applicability, not cost: with sequential scans priced out, a plan
        # that still scans means the predicate cannot use the primary key.
        conn.execute("SET enable_seqscan = off")
        for ws, user in (pairs[7], pairs[611], pairs[-1]):
            params = PostgresStore._chat_browser_key_index_range(ws, user)
            plan = conn.execute("EXPLAIN (FORMAT JSON) " + _CHAT_BROWSER_KEY_INDEX_SQL, params).fetchone()[0]
            node = plan[0]["Plan"]
            while node.get("Plans") and "Index" not in node["Node Type"]:
                node = node["Plans"][0]
            assert "Index" in node["Node Type"], (which, databases["default_collation"], plan)
            assert node.get("Index Name") == "tr_entities_pkey", plan
            condition = node.get("Index Cond", "")
            assert "id >=" in condition and "id <" in condition, condition

            rows = conn.execute(_CHAT_BROWSER_KEY_INDEX_SQL, params).fetchall()
            assert sorted(row[1] for row in rows) == sorted(
                f"key_{ws}_{user}_{i}_AbC" for i in range(5)
            ), which


def test_bounds_bracket_exactly_the_digest_prefix() -> None:
    """Pure check, no server: the upper bound is the digest plus one."""
    prefix = chat_browser_key_index_prefix("ws_a", "user_B")
    assert len(prefix) == 64 and set(prefix) <= set("0123456789abcdef")
    upper = chat_browser_key_index_upper(prefix)
    assert int(upper, 16) == int(prefix, 16) + 1
    assert prefix <= chat_browser_key_index_id("ws_a", "user_B", "key_Zz_9") < upper
    assert chat_browser_key_index_upper("f" * 64) == "g"
    assert "LIKE" not in _CHAT_BROWSER_KEY_INDEX_SQL
