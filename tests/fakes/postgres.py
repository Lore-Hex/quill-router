"""A psycopg-shaped SQLite connection, for running the store's REAL SQL.

Why this exists rather than an InMemory twin
--------------------------------------------
Two properties cannot be tested against `InMemoryStore` even in principle:

* **Atomicity.** A dict-and-lock store has no way to fail half-way, so a test
  that a partial write rolls back would pass against an implementation with no
  transaction at all.
* **The statements themselves.** The money guarantees on the Postgres backend
  live inside SQL — `ON CONFLICT DO NOTHING`, a conditional `UPDATE ... WHERE
  total_credits - total_usage - reserved >= %s`, and the `rowcount` checks that
  read them. A twin re-implements those in Python and proves nothing about the
  text that actually ships.

SQLite runs both: real BEGIN/ROLLBACK/COMMIT, real `ON CONFLICT`, real
rowcount on DML. The schema comes from the file `apply_schema()` sends, so a
column rename breaks these tests instead of letting them pass against a
fiction.

What it does NOT cover: Aurora DSQL's optimistic-concurrency aborts, real
cross-connection isolation, and `%s`/`::jsonb` dialect differences (translated
away below). Those need the live backends the conformance suite reaches when
TR_CONFORMANCE_POSTGRES_DSN is set. This harness is the floor, not the ceiling.
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any


class SqlitePostgresConn:
    """Psycopg-shaped adapter over SQLite."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw
        #: When set, any statement containing this substring raises — standing
        #: in for a crash or connection reset partway through a transaction,
        #: after earlier statements in it have already run.
        self.fail_on: str | None = None

    def execute(self, sql: str, params: tuple[Any, ...] = (), **_kwargs: Any) -> Any:
        if self.fail_on is not None and self.fail_on in sql:
            raise RuntimeError("connection reset mid-transaction")
        # `FOR UPDATE` is stripped, not honoured: SQLite has no row locks, and
        # this harness runs one connection, so there is nothing to serialize
        # against. Every guarantee under test here therefore has to hold
        # WITHOUT the lock — which is the point, since row-lock behaviour is
        # what differs most between plain Postgres and Aurora DSQL.
        translated = (
            sql.replace("%s", "?").replace("::jsonb", "").replace(" FOR UPDATE", "")
            # SQLite's scalar max is multi-arg MAX(); it has no GREATEST.
            # GREATEST always takes >=2 args here, so the mapping is exact —
            # single-arg MAX (the aggregate) can never be produced by it.
            .replace("GREATEST(", "MAX(")
        )
        return self._raw.execute(translated, params)

    def transaction(self) -> Any:
        """All-or-nothing, like `psycopg`'s `with conn.transaction()`."""

        @contextlib.contextmanager
        def _txn() -> Iterator[SqlitePostgresConn]:
            self._raw.execute("BEGIN")
            try:
                yield self
            except BaseException:
                self._raw.execute("ROLLBACK")
                raise
            self._raw.execute("COMMIT")

        return _txn()

    # --- inspection helpers -------------------------------------------------

    def has_entity(self, kind: str, entity_id: str) -> bool:
        return (
            self._raw.execute(
                "SELECT 1 FROM tr_entities WHERE kind = ? AND id = ?", (kind, entity_id)
            ).fetchone()
            is not None
        )

    def count_entities(self, kind: str) -> int:
        return int(
            self._raw.execute(
                "SELECT count(*) FROM tr_entities WHERE kind = ?", (kind,)
            ).fetchone()[0]
        )

    def balance(self, workspace_id: str) -> tuple[int, int, int] | None:
        row = self._raw.execute(
            "SELECT total_credits, total_usage, reserved FROM tr_credit_balance "
            "WHERE workspace_id = ? AND shard = 0",
            (workspace_id,),
        ).fetchone()
        return None if row is None else (int(row[0]), int(row[1]), int(row[2]))

    def spendable(self, workspace_id: str) -> int:
        row = self.balance(workspace_id)
        return 0 if row is None else row[0] - row[1] - row[2]

    def balance_row_count(self) -> int:
        return int(self._raw.execute("SELECT count(*) FROM tr_credit_balance").fetchone()[0])


#: Tables these tests need. Named explicitly so adding one is a deliberate act
#: rather than a silent widening of what the harness pretends to cover.
SCHEMA_TABLES = (
    "CREATE TABLE IF NOT EXISTS tr_entities",
    "CREATE TABLE IF NOT EXISTS tr_credit_balance",
    "CREATE TABLE IF NOT EXISTS tr_earnings_balance",
    "CREATE TABLE IF NOT EXISTS tr_credit_movement",
    "CREATE TABLE IF NOT EXISTS tr_user_lifetime_topup",
    "CREATE TABLE IF NOT EXISTS tr_trust_event",
    "CREATE UNIQUE INDEX IF NOT EXISTS tr_trust_event_adverse_dedup",
    "CREATE UNIQUE INDEX IF NOT EXISTS tr_trust_event_payment_dedup",
    "CREATE TABLE IF NOT EXISTS tr_key_limit",
    # Deferred settlement. The cap's whole claim to being a real bound is the
    # predicate on its UPDATE and the rowcount that reads it, and the outbox's
    # exactly-once claim is an ON CONFLICT — statement-level guarantees, which
    # is exactly the class this harness exists to run rather than re-implement.
    "CREATE TABLE IF NOT EXISTS tr_deferred_outstanding",
    "CREATE TABLE IF NOT EXISTS tr_home_settlement_outbox",
)


def schema_statements() -> list[str]:
    """The CREATE TABLEs, taken from the file the store actually applies.

    The rest of the schema (indexes, the DSQL-shaped `ALTER TABLE ... ADD
    COLUMN IF NOT EXISTS` upgrade path) is deliberately skipped: SQLite rejects
    that ALTER spelling, and neither indexes nor the upgrade path affects the
    transactional semantics under test.
    """
    import trusted_router.storage_postgres as storage_postgres

    schema = Path(storage_postgres.__file__).with_name("storage_postgres_schema.sql").read_text()
    statements = [
        statement
        for statement in storage_postgres._split_sql_statements(schema)
        if statement.lstrip().startswith(SCHEMA_TABLES)
    ]
    if len(statements) != len(SCHEMA_TABLES):
        raise AssertionError(
            f"schema file no longer yields every expected table: {statements}"
        )
    return statements


def sqlite_postgres_conn() -> SqlitePostgresConn:
    raw = sqlite3.connect(":memory:")
    raw.isolation_level = None  # explicit BEGIN/COMMIT, like psycopg
    for statement in schema_statements():
        raw.execute(statement)
    return SqlitePostgresConn(raw)


def postgres_store_on(conn: SqlitePostgresConn) -> Any:
    """A `PostgresStore` whose transactions land in `conn`.

    Built with `__new__` so no connection pool is opened: what is under test is
    the store's SQL and its transaction structure, which is all the atomicity
    and idempotency claims rest on.
    """
    from trusted_router.storage_postgres import PostgresStore

    store = PostgresStore.__new__(PostgresStore)
    store._run_transaction = lambda operation: _run(conn, operation)  # type: ignore[method-assign]
    return store


def _run(conn: SqlitePostgresConn, operation: Any) -> Any:
    with conn.transaction():
        return operation(conn)
