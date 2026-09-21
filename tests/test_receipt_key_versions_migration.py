from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from tests.deploy_script_harness import SCRIPT_FIXTURES, DeployScriptHarness, ScriptFixture
from tests.test_spend_lease_migration import _ddls
from trusted_router.storage_postgres import PostgresStore, _split_sql_statements

ROOT = Path(__file__).parents[1]
SCRIPT = "scripts/deploy/migrate_receipt_key_versions.sh"


class DsqlConnection:
    """Reject unsupported DSQL DDL without opening a database connection."""

    def __init__(self) -> None:
        self.autocommit = False
        self.accepted: list[str] = []

    def execute(self, ddl: str, *, prepare: bool) -> None:
        assert prepare is False
        normalized = " ".join(ddl.upper().split())
        if "ADD COLUMN" in normalized and re.search(
            r"\b(?:DEFAULT|NOT NULL|CHECK|REFERENCES)\b", normalized
        ):
            raise psycopg.errors.FeatureNotSupported(
                "ALTER TABLE ADD COLUMN with constraint not supported"
            )
        if re.match(r"CREATE (?:UNIQUE )?INDEX\b", normalized):
            if not re.match(r"CREATE (?:UNIQUE )?INDEX ASYNC\b", normalized):
                raise psycopg.errors.FeatureNotSupported(
                    "unsupported mode. please use CREATE INDEX ASYNC."
                )
            if re.search(r"\bWHERE\b", normalized):
                raise psycopg.errors.FeatureNotSupported("partial indexes not supported")
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
        normalized = " ".join(statement.upper().split())
        if normalized.startswith("ALTER TABLE ") and "ADD COLUMN" in normalized:
            assert not re.search(r"\b(?:DEFAULT|NOT NULL|CHECK|REFERENCES)\b", normalized), (
                "DSQL forbids constraints in ALTER TABLE ADD COLUMN; add the bare "
                f"column, then SET DEFAULT separately: {statement}"
            )
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

        def execute(self, ddl: str, *, prepare: bool) -> None:
            assert prepare is False
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
