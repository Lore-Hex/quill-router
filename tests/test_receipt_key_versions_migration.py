from __future__ import annotations

import contextlib
import logging
import os
import re
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psycopg
import pytest

from tests.deploy_script_harness import SCRIPT_FIXTURES, DeployScriptHarness, ScriptFixture
from tests.test_spend_lease_migration import _ddls
from trusted_router import storage_postgres
from trusted_router.storage_postgres import (
    PostgresStore,
    _schema_unique_index_wait_seconds,
    _split_sql_statements,
)

ROOT = Path(__file__).parents[1]
SCRIPT = "scripts/deploy/migrate_receipt_key_versions.sh"

_ADD_COLUMN_HEAD = re.compile(
    r"\s*ALTER\s+TABLE\s+\w+\s+ADD\s+COLUMN\s+(?:IF\s+NOT\s+EXISTS\s+)?",
    re.IGNORECASE,
)
_BARE_ADD_COLUMN = re.compile(
    _ADD_COLUMN_HEAD.pattern
    + r"\w+\s+(?:TEXT|BIGINT|INTEGER|BOOLEAN|TIMESTAMPTZ|JSONB|DOUBLE\s+PRECISION|"
    r"NUMERIC(?:\s*\(\s*\d+\s*,\s*\d+\s*\))?)\s*;?\s*",
    re.IGNORECASE,
)
_DSQL_COLUMN_RULE = (
    "DSQL requires a bare ADD COLUMN name and type with no constraint or other tail; "
    "use bare ADD COLUMN, then ALTER COLUMN SET DEFAULT"
)
_VALIDITY_QUERY = (
    "SELECT i.indisvalid FROM pg_index i "
    "JOIN pg_class c ON c.oid = i.indexrelid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = %s AND n.nspname = current_schema()"
)


def _assert_dsql_add_column(statement: str) -> None:
    if _ADD_COLUMN_HEAD.match(statement):
        assert _BARE_ADD_COLUMN.fullmatch(statement), f"{_DSQL_COLUMN_RULE}: {statement}"


@pytest.fixture(autouse=True)
def isolated_schema_wait_budget(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.delenv("TR_SCHEMA_UNIQUE_INDEX_WAIT_SECONDS", raising=False)
    _schema_unique_index_wait_seconds.cache_clear()
    yield
    _schema_unique_index_wait_seconds.cache_clear()


class DsqlConnection:
    """Reject unsupported DSQL DDL without opening a database connection."""

    def __init__(
        self,
        *,
        validity: dict[str, list[bool | None | psycopg.Error]] | None = None,
        existing_indexes: tuple[str, ...] = (),
    ) -> None:
        self.autocommit = False
        self.accepted: list[str] = []
        self.validity = validity or {}
        self.polls: list[str] = []
        self.indexes = set(existing_indexes)
        self.created_indexes: list[str] = []

    def execute(self, ddl: str, params: tuple[str, ...] = (), *, prepare: bool) -> Any:
        assert prepare is False
        if "pg_index" in ddl:
            assert ddl == _VALIDITY_QUERY
            name, = params
            self.polls.append(name)
            script = self.validity.get(name, [True])
            valid = script.pop(0) if len(script) > 1 else script[0]
            if isinstance(valid, psycopg.Error):
                raise valid
            return SimpleNamespace(fetchone=lambda: None if valid is None else (valid,))
        normalized = " ".join(ddl.upper().split())
        if _ADD_COLUMN_HEAD.match(ddl) and not _BARE_ADD_COLUMN.fullmatch(ddl):
            raise psycopg.errors.FeatureNotSupported(_DSQL_COLUMN_RULE)
        if re.match(r"CREATE (?:UNIQUE )?INDEX\b", normalized):
            if re.match(r"CREATE (?:UNIQUE )?INDEX ASYNC ASYNC\b", normalized):
                raise psycopg.errors.SyntaxError("INDEX ASYNC ASYNC is not valid DSQL")
            if not re.match(r"CREATE (?:UNIQUE )?INDEX ASYNC\b", normalized):
                raise psycopg.errors.FeatureNotSupported(
                    "unsupported mode. please use CREATE INDEX ASYNC."
                )
            if re.search(r"\bWHERE\b", normalized):
                raise psycopg.errors.FeatureNotSupported("partial indexes not supported")
            name = re.sub(
                r"^CREATE (?:UNIQUE )?INDEX ASYNC (?:IF NOT EXISTS )?", "", normalized,
            ).split()[0].lower()
            if name not in self.indexes:
                self.indexes.add(name)
                self.created_indexes.append(name)
        self.accepted.append(ddl)


def test_postgres_entire_schema_applies_on_dsql() -> None:
    conn = DsqlConnection()
    store = PostgresStore.__new__(PostgresStore)
    store._pool = SimpleNamespace(connection=lambda: contextlib.nullcontext(conn))
    store.apply_schema()

    statements = _split_sql_statements(
        (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()
    )
    assert len(conn.accepted) == len(statements)
    assert conn.autocommit is False
    for name in ("tr_trust_event_adverse_dedup", "tr_trust_event_payment_dedup"):
        assert any(
            ddl.startswith(f"CREATE UNIQUE INDEX ASYNC IF NOT EXISTS {name}\n")
            for ddl in conn.accepted
        )
    assert (
        "CREATE INDEX ASYNC IF NOT EXISTS tr_receipt_key_versions\n"
        "    ON tr_entities (kid, att_sha256)"
    ) in conn.accepted


def test_postgres_schema_add_column_obeys_dsql_constraint_rule() -> None:
    statements = _split_sql_statements(
        (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()
    )
    for statement in statements:
        _assert_dsql_add_column(statement)
    for column in ("trust_tier", "pause_epoch"):
        add = f"ALTER TABLE tr_credit_balance ADD COLUMN IF NOT EXISTS {column} BIGINT"
        default = f"ALTER TABLE tr_credit_balance ALTER COLUMN {column} SET DEFAULT 0"
        assert statements.index(add) < statements.index(default)


@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize("error", [None, psycopg.errors.FeatureNotSupported,
                                  psycopg.errors.SyntaxError, psycopg.errors.UniqueViolation])
def test_postgres_index_fallback_only_on_feature_not_supported(
    unique: bool, error: type[psycopg.Error] | None,
) -> None:
    prefix = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
    statement = f"{prefix} IF NOT EXISTS example ON tr_entities (id)"

    class Connection:
        def __init__(self) -> None:
            self.calls: list[str] = []
            self.polls: list[tuple[str, tuple[str, ...]]] = []

        def execute(self, ddl: str, params: tuple[str, ...] = (), *, prepare: bool) -> Any:
            assert prepare is False
            if "pg_index" in ddl:
                self.polls.append((ddl, params))
                return SimpleNamespace(fetchone=lambda: (True,))
            self.calls.append(ddl)
            if error is not None and len(self.calls) == 1:
                raise error("DDL rejected")

    conn = Connection()
    if error not in (None, psycopg.errors.FeatureNotSupported):
        with pytest.raises(error):
            PostgresStore._execute_ddl(conn, statement)
    else:
        PostgresStore._execute_ddl(conn, statement)
    expected = [statement]
    if error is psycopg.errors.FeatureNotSupported:
        expected.append(f"{prefix} ASYNC IF NOT EXISTS example ON tr_entities (id)")
    assert conn.calls == expected
    assert conn.polls == (
        [(_VALIDITY_QUERY, ("example",))]
        if unique and error is psycopg.errors.FeatureNotSupported else []
    )


@pytest.fixture
def wait_sleeps(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    monkeypatch.setenv("TR_SCHEMA_UNIQUE_INDEX_WAIT_SECONDS", "3")
    monkeypatch.setattr(storage_postgres.time, "monotonic", lambda: 0.0)
    sleeps: list[int] = []

    def sleep(seconds: int) -> None:
        sleeps.append(seconds)
        assert seconds == 1
        assert len(sleeps) <= 3, "DSQL validity wait must terminate with a stubbed sleep"

    monkeypatch.setattr(storage_postgres.time, "sleep", sleep)
    return sleeps


@pytest.mark.parametrize("statement", [
    "CREATE UNIQUE INDEX IF NOT EXISTS example ON tr_entities (id)",
    "create\nunique index example ON tr_entities (id)",
    'CREATE UNIQUE INDEX IF NOT EXISTS "example" ON tr_entities (id)',
])
def test_postgres_dsql_unique_index_waits_until_valid(
    statement: str, wait_sleeps: list[int], caplog: pytest.LogCaptureFixture,
) -> None:
    conn = DsqlConnection(validity={"example": [False, False, True]})
    PostgresStore._execute_ddl(conn, statement)
    assert conn.polls == ["example"] * 3
    assert wait_sleeps == [1, 1]
    assert not caplog.records


@pytest.mark.parametrize("validity", [False, None])
def test_postgres_dsql_unique_index_timeout_still_applies_rest_of_schema(
    validity: bool | None, wait_sleeps: list[int], caplog: pytest.LogCaptureFixture,
) -> None:
    name = "tr_trust_event_adverse_dedup"
    conn = DsqlConnection(validity={name: [validity]})
    store = PostgresStore.__new__(PostgresStore)
    store._pool = SimpleNamespace(connection=lambda: contextlib.nullcontext(conn))
    store.apply_schema()
    statements = _split_sql_statements(
        (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()
    )
    assert len(conn.accepted) == len(statements)
    assert conn.accepted[-1].replace("INDEX ASYNC", "INDEX", 1) == statements[-1]
    assert conn.autocommit is False
    assert conn.polls == [name] * 4 + ["tr_trust_event_payment_dedup"]
    assert wait_sleeps == [1, 1, 1]
    assert len(caplog.records) == 1
    record, = caplog.records
    assert record.levelno == logging.ERROR
    message = record.getMessage()
    assert name in message
    assert "writes depending on it will be refused until it is valid" in message
    assert "SELECT * FROM sys.jobs" in message
    assert "drop and recreate a failed build" in message
    assert "\n" not in message


def test_postgres_dsql_existing_valid_index_is_polled_once(
    wait_sleeps: list[int], caplog: pytest.LogCaptureFixture,
) -> None:
    conn = DsqlConnection(existing_indexes=("example",))
    PostgresStore._execute_ddl(
        conn, "CREATE UNIQUE INDEX IF NOT EXISTS example ON tr_entities (id)",
    )
    assert conn.created_indexes == []
    assert conn.polls == ["example"]
    assert wait_sleeps == []
    assert not caplog.records


@pytest.mark.parametrize("recovers", [False, True])
def test_postgres_dsql_catalog_error_warns_once_and_does_not_raise(
    recovers: bool, wait_sleeps: list[int], caplog: pytest.LogCaptureFixture,
) -> None:
    error = psycopg.errors.InsufficientPrivilege("catalog unavailable")
    conn = DsqlConnection(validity={"example": [error, error, True] if recovers else [error]})
    PostgresStore._execute_ddl(
        conn, "CREATE UNIQUE INDEX IF NOT EXISTS example ON tr_entities (id)",
    )
    assert conn.polls == ["example"] * (3 if recovers else 4)
    assert wait_sleeps == [1] * (2 if recovers else 3)
    assert [record.levelno for record in caplog.records] == (
        [logging.WARNING] if recovers else [logging.WARNING, logging.ERROR]
    )
    assert all("example" in record.getMessage() for record in caplog.records)


def test_postgres_dsql_non_unique_index_does_not_poll(wait_sleeps: list[int]) -> None:
    conn = DsqlConnection(validity={"example": [False]})
    PostgresStore._execute_ddl(
        conn, "CREATE INDEX IF NOT EXISTS example ON tr_entities (id)",
    )
    assert conn.polls == []
    assert wait_sleeps == []


@pytest.mark.parametrize("unique", [False, True])
@pytest.mark.parametrize("async_token", ["ASYNC", "async\n"])
def test_postgres_already_async_feature_not_supported_preserves_original_error(
    unique: bool, async_token: str,
) -> None:
    error = psycopg.errors.FeatureNotSupported("some other unsupported feature")
    calls: list[str] = []

    class Connection:
        def execute(self, ddl: str, *, prepare: bool) -> None:
            calls.append(ddl)
            if len(calls) == 1:
                raise error
            raise psycopg.errors.SyntaxError("INDEX ASYNC ASYNC")

    prefix = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
    statement = f"{prefix} {async_token} IF NOT EXISTS example ON tr_entities (id)"
    with pytest.raises(psycopg.errors.FeatureNotSupported) as raised:
        PostgresStore._execute_ddl(Connection(), statement)
    assert raised.value is error
    assert calls == [statement]


@pytest.mark.parametrize("unique", [False, True])
def test_postgres_dsql_fake_rejects_double_async(unique: bool) -> None:
    prefix = "CREATE UNIQUE INDEX" if unique else "CREATE INDEX"
    conn = DsqlConnection()
    with pytest.raises(psycopg.errors.SyntaxError, match="INDEX ASYNC ASYNC"):
        conn.execute(f"{prefix} ASYNC ASYNC example ON tr_entities (id)", prepare=False)
    assert conn.accepted == []


@pytest.mark.parametrize("value", ["0", "abc", "-1", "1.5"])
def test_postgres_dsql_unique_index_wait_rejects_invalid_budget(
    value: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TR_SCHEMA_UNIQUE_INDEX_WAIT_SECONDS", value)
    with pytest.raises(ValueError, match="TR_SCHEMA_UNIQUE_INDEX_WAIT_SECONDS"):
        PostgresStore._execute_ddl(
            DsqlConnection(), "CREATE UNIQUE INDEX example ON tr_entities (id)",
        )


def test_postgres_dsql_unique_index_wait_budget_is_read_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert _schema_unique_index_wait_seconds() == 60
    monkeypatch.setenv("TR_SCHEMA_UNIQUE_INDEX_WAIT_SECONDS", "abc")
    assert _schema_unique_index_wait_seconds() == 60


@pytest.mark.parametrize("tail", [
    "DEFAULT 0", "NOT NULL", "NULL", "CHECK (value > 0)",
    "REFERENCES parent (id)", "UNIQUE", "PRIMARY KEY", "GENERATED ALWAYS AS IDENTITY",
    'COLLATE "C"', "CONSTRAINT positive CHECK (value > 0)",
])
@pytest.mark.parametrize("layout", ["uppercase", "lowercase", "multiline"])
@pytest.mark.parametrize("validator", ["guard", "fake"])
def test_postgres_dsql_add_column_forbidden_tails(
    tail: str, layout: str, validator: str,
) -> None:
    statement = f"ALTER TABLE example ADD COLUMN IF NOT EXISTS value BIGINT {tail};"
    if layout == "lowercase":
        statement = statement.lower()
    elif layout == "multiline":
        statement = "\n  ".join(statement.split())
    if validator == "guard":
        with pytest.raises(AssertionError, match="DSQL.*bare ADD COLUMN.*ALTER COLUMN SET DEFAULT"):
            _assert_dsql_add_column(statement)
    else:
        conn = DsqlConnection()
        with pytest.raises(
            psycopg.errors.FeatureNotSupported,
            match="DSQL.*bare ADD COLUMN.*ALTER COLUMN SET DEFAULT",
        ):
            conn.execute(statement, prepare=False)
        assert conn.accepted == []


@pytest.mark.parametrize("column_type", [
    "TEXT", "BIGINT", "INTEGER", "BOOLEAN", "TIMESTAMPTZ", "JSONB", "DOUBLE PRECISION",
    "NUMERIC", "NUMERIC(12, 3)",
])
@pytest.mark.parametrize("layout", ["uppercase", "lowercase", "multiline"])
def test_postgres_dsql_add_column_bare_allowed_types(column_type: str, layout: str) -> None:
    statement = f"ALTER TABLE example ADD COLUMN value {column_type};"
    if layout == "lowercase":
        statement = statement.lower()
    elif layout == "multiline":
        statement = "\n  ".join(statement.split())
    _assert_dsql_add_column(statement)
    conn = DsqlConnection()
    conn.execute(statement, prepare=False)
    assert conn.accepted == [statement]


def test_postgres_dsql_add_column_type_must_be_allowlisted() -> None:
    statement = "ALTER TABLE example ADD COLUMN value VARCHAR(20)"
    with pytest.raises(AssertionError, match="DSQL"):
        _assert_dsql_add_column(statement)
    with pytest.raises(psycopg.errors.FeatureNotSupported, match="DSQL"):
        DsqlConnection().execute(statement, prepare=False)


def test_postgres_non_index_feature_not_supported_is_not_retried() -> None:
    calls: list[str] = []
    error = psycopg.errors.FeatureNotSupported("unsupported column constraint")

    class Connection:
        def execute(self, ddl: str, *, prepare: bool) -> None:
            calls.append(ddl)
            raise error

    statement = "ALTER TABLE example ADD COLUMN value BIGINT DEFAULT 0"
    with pytest.raises(psycopg.errors.FeatureNotSupported) as raised:
        PostgresStore._execute_ddl(Connection(), statement)
    assert raised.value is error
    assert calls == [statement]


def test_spanner_receipt_key_version_migration_is_additive_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = ROOT / SCRIPT
    assert os.access(path, os.X_OK)
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        ScriptFixture(
            env={
                "GCP_PROJECT_ID": "test-project",
                "SPANNER_INSTANCE_ID": "test-instance",
                "SPANNER_DATABASE_ID": "test-database",
            },
            responses=(
                (r"INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES", "READ_WRITE"),
                (r"INFORMATION_SCHEMA\.(COLUMNS|INDEXES)", "0"),
            ),
        ),
    )

    run = DeployScriptHarness(tmp_path).run(SCRIPT)

    assert run.returncode == 0, run.stderr
    assert _ddls(run) == [
        "ALTER TABLE tr_entities ADD COLUMN kid STRING(43)",
        "ALTER TABLE tr_entities ADD COLUMN att_sha256 STRING(43)",
        "CREATE NULL_FILTERED INDEX tr_receipt_key_versions "
        "ON tr_entities (kid, att_sha256)",
    ]


def test_spanner_migration_existing_read_write_index_is_a_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        ScriptFixture(
            env={
                "GCP_PROJECT_ID": "test-project",
                "SPANNER_INSTANCE_ID": "test-instance",
                "SPANNER_DATABASE_ID": "test-database",
            },
            responses=(
                (r"INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES", "READ_WRITE"),
                (r"INFORMATION_SCHEMA\.(COLUMNS|INDEXES)", "1"),
            ),
        ),
    )

    run = DeployScriptHarness(tmp_path).run(SCRIPT)

    assert run.returncode == 0, run.stderr
    assert _ddls(run) == []
    assert not any(call[0] == "sleep" for call in run.calls)


@pytest.mark.parametrize("state", ["CREATING", "WRITE_ONLY"])
def test_spanner_migration_times_out_waiting_for_unready_existing_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        ScriptFixture(
            env={
                "GCP_PROJECT_ID": "test-project",
                "SPANNER_INSTANCE_ID": "test-instance",
                "SPANNER_DATABASE_ID": "test-database",
                "RECEIPT_KEY_INDEX_WAIT_ATTEMPTS": "3",
                "RECEIPT_KEY_INDEX_WAIT_SECONDS": "0",
            },
            responses=(
                (r"INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES", state),
                (r"INFORMATION_SCHEMA\.(COLUMNS|INDEXES)", "1"),
            ),
        ),
    )

    run = DeployScriptHarness(tmp_path).run(SCRIPT)

    assert run.returncode != 0
    assert _ddls(run) == []
    assert sum(call[0] == "sleep" for call in run.calls) == 3
    assert (
        "ERROR: timed out waiting for tr_receipt_key_versions to become READ_WRITE "
        f"after 3 attempts (last state={state})"
    ) in run.stdout


def test_postgres_runtime_schema_migrates_nullable_receipt_version_index() -> None:
    schema = (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()

    assert "ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS kid TEXT;" in schema
    assert "ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS att_sha256 TEXT;" in schema
    index = (
        "CREATE INDEX IF NOT EXISTS tr_receipt_key_versions\n"
        "    ON tr_entities (kid, att_sha256)\n"
        "    WHERE kid IS NOT NULL AND att_sha256 IS NOT NULL;"
    )
    assert index in schema
    assert schema.index("ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS att_sha256 TEXT;") < (
        schema.index(index)
    )
    assert "kid TEXT NOT NULL" not in schema
    assert "att_sha256 TEXT NOT NULL" not in schema


def test_receipt_version_index_ddl_is_exact_for_postgres_and_dsql() -> None:
    schema = (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()
    statement = next(
        item
        for item in _split_sql_statements(schema)
        if item.startswith("CREATE INDEX IF NOT EXISTS tr_receipt_key_versions")
    )
    stock_expected = (
        "CREATE INDEX IF NOT EXISTS tr_receipt_key_versions\n"
        "    ON tr_entities (kid, att_sha256)\n"
        "    WHERE kid IS NOT NULL AND att_sha256 IS NOT NULL"
    )
    dsql_expected = (
        "CREATE INDEX ASYNC IF NOT EXISTS tr_receipt_key_versions\n"
        "    ON tr_entities (kid, att_sha256)"
    )
    assert statement == stock_expected

    class Connection:
        def __init__(self, *, dsql: bool) -> None:
            self.dsql = dsql
            self.calls: list[tuple[str, bool]] = []

        def execute(self, ddl: str, *, prepare: bool) -> None:
            self.calls.append((ddl, prepare))
            if self.dsql and len(self.calls) == 1:
                raise psycopg.errors.FeatureNotSupported(
                    "unsupported mode. please use CREATE INDEX ASYNC."
                )

    stock = Connection(dsql=False)
    PostgresStore._execute_ddl(stock, statement)
    assert stock.calls == [(stock_expected, False)]

    dsql = Connection(dsql=True)
    PostgresStore._execute_ddl(dsql, statement)
    assert dsql.calls == [(stock_expected, False), (dsql_expected, False)]


def test_deploy_applies_receipt_version_schema_before_router_rollout() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    migrate = workflow.index("scripts/deploy/migrate_receipt_key_versions.sh")
    backfill = workflow.index("python -m trusted_router.receipt_key_backfill_cli")
    deploy = workflow.index("\n  deploy:\n")

    assert migrate < backfill < deploy
    assert "TR_STORAGE_BACKEND: spanner-clickhouse" in workflow[migrate:backfill]


def test_receipt_docs_require_ddl_before_the_new_writer() -> None:
    docs = (ROOT / "docs/client-receipts.md").read_text()

    assert "Apply that DDL and\nbackfill before starting this router revision" in docs
    assert "writer names the physical `kid`\nand `att_sha256` columns" in docs
    assert "bounded, resumable `trusted_router.receipt_key_backfill_cli`" in docs
    assert "safe to run\nbefore or after the compatible router" not in docs


def test_public_receipt_docs_match_listing_and_version_lookup_contracts() -> None:
    docs = (ROOT / "docs/client-receipts.md").read_text()
    page = (ROOT / "src/trusted_router/templates/public/receipts.html").read_text()

    assert "ordered by immutable `(kid, att_sha256)`" in docs
    assert "pages\nof at most 250 versions and at most 1 MiB" in docs
    assert "every retained attestation version for exactly one\nsigning key" in docs
    assert "ordered by immutable <code>(kid, att_sha256)</code>" in page
    assert "pages of at most 250 versions and capped at 1 MiB per response" in page
    assert "Every retained attestation version for exactly one signing key" in page
    assert "The same byte and page ceilings apply" in docs
    assert "with the same page and byte ceilings" in page
    assert "every observed attestation re-mint for every signing key" not in page
