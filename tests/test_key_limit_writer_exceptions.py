"""Behavioral pins for the reviewed exceptions to the limit-column check.

The writer inventory's backstop (tests/key_limit_writers.py) counts any scope
that holds the key-limit table, a limit column and a write as writing every
limit column, whatever the parser reads; a scope that writes no limit column
is listed in tests/test_key_limit_writer_inventory.py LIMIT_COLUMN_EXCEPTIONS,
pinned to a fingerprint of its reviewed source, and proven here by running it:
every statement it sends, and every mutation it makes, is captured and holds
no limit column (or, for the reshard and the escrow rebalance, holds them
only through the chokepoint).
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes.spanner import _ParamTypes
from tests.key_limit_writers import (
    LIMIT_COLUMNS,
    ROOT,
    _statements,
    _strip_comments,
    names_table,
    sql_tokens,
    written_columns,
)


def _assert_writes_no_limit(statements: list[str]) -> None:
    assert statements
    for sql in statements:
        columns = written_columns(sql)
        assert columns is not None, sql
        assert not columns & LIMIT_COLUMNS, (sql, columns)


class _Capture:
    """A transaction or connection that records every statement and mutation."""

    def __init__(self, *, count: int = 1, rows: list[tuple[Any, ...]] | None = None) -> None:
        self.statements: list[str] = []
        self.mutations: list[tuple[str, tuple[str, ...]]] = []
        self.count = count
        self.rows = rows or []

    def execute_update(self, sql: str, *args: Any, **kwargs: Any) -> int:
        self.statements.append(sql)
        return self.count

    def execute_sql(self, sql: str, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        self.statements.append(sql)
        return list(self.rows)

    def batch_update(self, statements: list[Any], *args: Any, **kwargs: Any) -> tuple[Any, list[int]]:
        self.statements += [s[0] if isinstance(s, tuple) else s for s in statements]
        return None, [self.count] * len(statements)

    def _mutate(self, table: str, columns: Any, values: Any) -> None:
        self.mutations.append((table, tuple(columns)))

    insert = update = insert_or_update = replace = _mutate


# --- storage_gcp_counter_dml ------------------------------------------------


@pytest.mark.parametrize("is_byok", [False, True])
@pytest.mark.parametrize("exclusive", [False, True])
def test_the_key_reserve_statement_assigns_reserved_only(is_byok: bool, exclusive: bool) -> None:
    from trusted_router.storage_gcp_counter_dml import reserve_key_statement

    sql, _params, _types = reserve_key_statement(_ParamTypes(), "kh", 5, is_byok=is_byok, exclusive=exclusive)
    assert written_columns(sql) == {"reserved"}


@pytest.mark.parametrize(("count", "rows"), [(1, []), (0, []), (0, [(None, True)]), (0, [(10, False)]),
                                             (0, [(10, True)])])
def test_the_key_reserve_writes_no_limit(count: int, rows: list[tuple[Any, ...]]) -> None:
    from trusted_router.storage_gcp_counter_dml import reserve_key

    for is_byok in (False, True):
        transaction = _Capture(count=count, rows=rows)
        reserve_key(transaction, _ParamTypes(), "kh", 5, is_byok=is_byok)
        _assert_writes_no_limit(transaction.statements)
        assert transaction.mutations == []


# --- storage_gcp_strict_budget -----------------------------------------------


@pytest.mark.parametrize("count", [0, 1])
@pytest.mark.parametrize("is_byok", [False, True])
@pytest.mark.parametrize("enforce_windows", [False, True])
def test_the_strict_reserve_writes_no_limit(count: int, is_byok: bool, enforce_windows: bool) -> None:
    from trusted_router.storage_gcp_strict_budget import reserve_strict_key

    row = (True, 0, 10, 0, None, 20, 0, None, 30, 0, None)
    transaction = _Capture(count=count, rows=[row])
    reserve_strict_key(transaction, _ParamTypes(), "kh", 5, is_byok=is_byok, enforce_windows=enforce_windows)
    _assert_writes_no_limit(transaction.statements)
    assert transaction.mutations == []


# --- storage_postgres --------------------------------------------------------


def _postgres_row(*, limit: int | None, strict: bool, alert_only: bool = False) -> tuple[Any, ...]:
    return (limit, 0, 0, 0, True, 10, 20, 30, 0, None, 0, None, 0, None,
            "true" if alert_only else "false", "true" if strict else "false")


@pytest.mark.parametrize(("limit", "strict", "alert_only"), [
    (10_000_000, False, False), (None, True, False), (10_000_000, True, False), (None, True, True)])
def test_the_postgres_reserve_writes_no_limit(monkeypatch: pytest.MonkeyPatch, limit: int | None, strict: bool,
                                              alert_only: bool) -> None:
    from trusted_router.storage_postgres import PostgresStore

    statements: list[str] = []
    row = _postgres_row(limit=limit, strict=strict, alert_only=alert_only)

    class Result:
        rowcount = 1

        def fetchone(self) -> tuple[Any, ...]:
            return row

    class Connection:
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> Result:
            statements.append(sql)
            return Result()

    store = PostgresStore.__new__(PostgresStore)
    store._supports_statement_timeout = True
    monkeypatch.setattr(PostgresStore, "_run_transaction", lambda _self, operation: operation(Connection()))
    store.reserve_key_limit("kh-pg", 1, usage_type="Credits")
    _assert_writes_no_limit([sql for sql in statements if names_table(sql)])
    assert any(sql.lstrip().upper().startswith("UPDATE TR_KEY_LIMIT") for sql in statements)


# --- storage_gcp_key_shard_admin ---------------------------------------------


def test_the_key_reshard_writes_limits_only_through_the_chokepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every tr_key_limit write naming a limit column is made inside
    storage_gcp_key_limit_mirror.write_key_limit_rows; the reshard's own
    writes (shard deletes, the entity) name none."""
    from tests.fakes import spanner as fake
    from tests.fakes.spanner import make_fake_store
    from trusted_router import storage_gcp_key_shard_admin as admin
    from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
    from trusted_router.storage_models import CreditAccount, Workspace

    inside = [False]
    writes: list[tuple[str, tuple[str, ...], bool]] = []
    real_chokepoint = admin.write_key_limit_rows

    def chokepoint(*args: Any, **kwargs: Any) -> None:
        inside[0] = True
        try:
            real_chokepoint(*args, **kwargs)
        finally:
            inside[0] = False

    def recording(method: Any) -> Any:
        def record(self: Any, *, table: str, columns: Any, values: Any) -> Any:
            writes.append((table, tuple(columns), inside[0]))
            return method(self, table=table, columns=columns, values=values)
        return record

    monkeypatch.setattr(admin, "write_key_limit_rows", chokepoint)
    for name in ("insert", "update", "insert_or_update"):
        monkeypatch.setattr(fake._FakeTransaction, name, recording(getattr(fake._FakeTransaction, name)))

    store, database = make_fake_store()
    store._write_entity("workspace", "ws-reshard", Workspace(
        id="ws-reshard", name="w", owner_user_id="owner", billing_paused=True))
    store._write_entity("credit", "ws-reshard", CreditAccount(workspace_id="ws-reshard"))
    database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[("ws-reshard", 0)] = {
        "workspace_id": "ws-reshard", "shard": 0, "total_credits": 1_000_000, "total_usage": 0,
        "reserved": 0, "source_updated_at": None, "updated_at": None,
    }
    _raw, key = store.api_keys.create(workspace_id="ws-reshard", name="k", creator_user_id=None,
                                      limit_microdollars=9_000_000, limit_daily_microdollars=1_000_000)
    writes.clear()
    status = admin.reshard_key_usage(store, key.hash, 2, apply=True)
    assert status.applied, status.reasons
    limit_writes = [(table, inside) for table, columns, inside in writes
                    if table == "tr_key_limit" and set(columns) & LIMIT_COLUMNS]
    assert limit_writes, "the reshard partitions the key's limits"
    assert all(inside for _table, inside in limit_writes)


# --- storage_gcp_key_escrow --------------------------------------------------


def test_the_key_escrow_moves_limits_only_through_the_chokepoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """rebalance_key_limit_headroom, judged with the functions nested in it
    (read_counters reads limit_micro; txn writes): a capped key's shard
    limits are written only inside write_key_limit_rows, their sum kept, and
    a key with any shard's limit unset (a limitless key) gets no write."""
    import copy

    import trusted_router.storage_gcp_key_escrow as escrow
    from tests.fakes import spanner as fake
    from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE

    inside = [False]
    writes: list[tuple[str, tuple[str, ...], bool]] = []
    real_chokepoint = escrow.write_key_limit_rows

    def chokepoint(*args: Any, **kwargs: Any) -> None:
        inside[0] = True
        try:
            real_chokepoint(*args, **kwargs)
        finally:
            inside[0] = False

    def recording(method: Any) -> Any:
        def record(self: Any, *, table: str, columns: Any, values: Any) -> Any:
            writes.append((table, tuple(columns), inside[0]))
            return method(self, table=table, columns=columns, values=values)
        return record

    monkeypatch.setattr(escrow, "write_key_limit_rows", chokepoint)
    for name in ("insert", "update", "insert_or_update", "replace"):
        if hasattr(fake._FakeTransaction, name):
            monkeypatch.setattr(fake._FakeTransaction, name, recording(getattr(fake._FakeTransaction, name)))

    def seeded(limits: list[int | None]) -> fake.FakeSpannerDatabase:
        database = fake.FakeSpannerDatabase()
        database.typed[KEY_LIMIT_TABLE] = {
            ("key", shard): {"key_hash": "key", "shard": shard, "limit_micro": limit, "usage": 0,
                             "byok_usage": 0, "reserved": 0, "include_byok": True}
            for shard, limit in enumerate(limits)
        }
        return database

    def rebalance(database: fake.FakeSpannerDatabase) -> bool:
        return escrow.rebalance_key_limit_headroom(database, _ParamTypes, key_hash="key", shard_count=4,
                                                   estimate=4, preferred_shard=0)

    capped = seeded([5, 3, 4, 4])
    assert rebalance(capped)
    limit_writes = [inside for table, columns, inside in writes
                    if table == KEY_LIMIT_TABLE and set(columns) & LIMIT_COLUMNS]
    assert limit_writes and all(limit_writes)
    assert sum(row["limit_micro"] for row in capped.typed[KEY_LIMIT_TABLE].values()) == 16
    writes.clear()
    limitless = seeded([None, None, None, None])
    before = copy.deepcopy(limitless.typed)
    assert not rebalance(limitless)
    assert writes == [] and limitless.typed == before


# --- storage_gcp_speculation_shadow ------------------------------------------


def test_the_speculation_shadow_resolve_only_reads_tr_key_limit() -> None:
    """resolve reads the key's limit rows in a read-only snapshot, and its
    transaction writes only the shadow's own records."""
    import hashlib
    from contextlib import contextmanager

    from google.cloud.spanner_v1 import param_types

    from trusted_router.config import Settings
    from trusted_router.services import speculation_shadow as shadow
    from trusted_router.storage_gcp_speculation_shadow import SpannerSpeculationShadow

    key = SimpleNamespace(hash="k", workspace_id="w", lookup_hash="a" * 64, usage_shard_count=1,
                          disabled=False, management=False, scopes=[], app_id="", federated_home="",
                          budget_strict=False, expires_at=None, limit_microdollars=None,
                          limit_daily_microdollars=None, limit_weekly_microdollars=None,
                          limit_monthly_microdollars=None)
    credits: list[list[Any]] = [[0, 20_000_000, 0, 0, 2, None, [], 0, dt.datetime.fromtimestamp(1900, dt.UTC)]]
    events = [["payment", "stripe", 20_000_000, 0, "succeeded", 0, 0]]
    statements: list[str] = []

    class Snapshot:
        """Reads only: it has no method that writes."""

        def read(self, table: str, columns: Any, keys: Any) -> list[Any]:
            return []

        def execute_sql(self, sql: str, **kwargs: Any) -> list[Any]:
            statements.append(sql)
            if "tr_credit_balance" in sql:
                return credits
            if "tr_key_limit" in sql:
                return [[0, None, None, None, None]]
            return events

    @contextmanager
    def snapshot(**kwargs: Any) -> Any:
        yield Snapshot()

    backend = SimpleNamespace(
        _database=SimpleNamespace(snapshot=snapshot), _param_types=param_types,
        get_key_by_lookup_hash=lambda lookup: key,
        _read_entity_from=lambda snap, kind, pk, cls: {
            "api_key_lookup": {"key_id": "k"}, "api_key": key, "credit": SimpleNamespace(shard_count=1),
            "workspace": SimpleNamespace()}[kind])
    store = SpannerSpeculationShadow(backend, Settings(environment="test"))
    anchor = dict(coverage_version="audited-v1", source_ledger_digest=hashlib.sha256(shadow.canonical(events)).hexdigest(),
                  source_credits_micro=20_000_000, source_as_of=1990, source_expires_at=2100)
    store.read = lambda table, pk: anchor
    kinds: list[str] = []

    class Recorder:
        def get(self, kind: str, pk: Any) -> Any:
            kinds.append(kind)
            return None

        def put(self, kind: str, pk: Any, value: Any) -> None:
            kinds.append(kind)

    store.transaction = lambda function: function(Recorder())
    store.resolve(key.lookup_hash, 2000)
    assert any(names_table(sql) for sql in statements)
    _assert_writes_no_limit(statements)
    assert set(kinds) <= {"paid", "scope"}


# --- scripts/backfill_leased_key_usage ---------------------------------------


def test_the_leased_usage_backfill_reads_key_counters_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """read_key_counters interpolates the key hash into a SELECT; the hash is
    checked against [A-Za-z0-9_-] first, so it cannot carry SQL."""
    import scripts.backfill_leased_key_usage as backfill

    seen: list[str] = []

    def recording_sql(sql: str, account: str) -> list[dict[str, int]]:
        seen.append(sql)
        return [{"usage": 0}]

    monkeypatch.setattr(backfill, "_sql", recording_sql)
    backfill.read_key_counters("kh_1", account="ops")
    _assert_writes_no_limit(seen)
    with pytest.raises(ValueError):
        backfill.read_key_counters("x'; UPDATE tr_key_limit SET limit_micro = 1 --", account="ops")
    assert len(seen) == 1


# --- fastpath/schema/fastpath.sql --------------------------------------------


def test_the_fast_path_schema_only_defines_tr_key_limit() -> None:
    """Its write words are DDL clauses elsewhere (ON DELETE CASCADE): every
    statement naming tr_key_limit is a CREATE or ALTER."""
    path = ROOT / "fastpath" / "schema" / "fastpath.sql"
    text = _strip_comments(path, path.read_text())
    assert written_columns(text) == set()
    tokens = sql_tokens(text)
    assert tokens is not None
    on_table = [statement for statement in _statements(tokens)
                if names_table(text[statement[0].start:statement[-1].end])]
    assert on_table and all(statement[0].text in ("create", "alter") for statement in on_table)


# --- scripts/deploy/migrate_typed_counters.sh --------------------------------

MIGRATION = ROOT / "scripts" / "deploy" / "migrate_typed_counters.sh"


def run_migration(tmp_path: Path, script: str) -> list[str]:
    """Run the migration with a recording gcloud and nothing else on PATH (a
    command it does not expect fails the run), and return every statement it
    sent: --sql and --ddl."""
    tools = tmp_path / "bin"
    tools.mkdir(parents=True)
    log = tmp_path / "gcloud.log"
    gcloud = tools / "gcloud"
    gcloud.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv[1:]\n"
        "open(os.environ['GCLOUD_LOG'], 'a').write(json.dumps(args) + '\\n')\n"
        "sql = next((a[6:] for a in args if a.startswith('--sql=')), '')\n"
        "print('COMMITTED' if 'SPANNER_STATE' in sql else 'READ_WRITE' if 'INDEX_STATE' in sql "
        "else '0' if sql else '')\n")
    gcloud.chmod(0o755)
    for tool in ("seq", "sleep"):
        found = shutil.which(tool)
        assert found
        (tools / tool).symlink_to(found)
    target = tmp_path / MIGRATION.name
    target.write_text(script)
    bash = shutil.which("bash")
    assert bash
    subprocess.run(  # noqa: S603 - the repository's script, against a recording stub
        [bash, str(target)], check=True, capture_output=True, text=True, timeout=120,
        env={"PATH": str(tools), "SPANNER_INSTANCE_ID": "i", "SPANNER_DATABASE_ID": "d", "GCLOUD_LOG": str(log)})
    statements = []
    for line in log.read_text().splitlines():
        for arg in json.loads(line):
            if arg.startswith(("--sql=", "--ddl=")):
                statements.append(arg[6:])
    return statements


def test_the_typed_counter_migration_only_defines_tr_key_limit(tmp_path: Path) -> None:
    """Its one write word is gcloud's "ddl update" subcommand: run with a
    recording gcloud, every statement it sends is a SELECT or DDL, and those
    naming tr_key_limit create it or add its columns."""
    statements = run_migration(tmp_path, MIGRATION.read_text())
    on_table = [sql for sql in statements if names_table(sql)]
    assert any("day_limit_micro" in sql for sql in on_table), "the run reaches the table's DDL"
    for sql in statements:
        assert written_columns(sql) == set(), sql
