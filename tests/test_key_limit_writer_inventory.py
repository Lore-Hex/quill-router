"""Every writer of tr_key_limit, held to a reviewed list, and the limit
columns written only by the guarded functions.

A strict key's tr_key_limit limits (limit_micro, day/week/month_limit_micro)
never go from every one unset to any set: Python's strict reserve and the fast
path edge read them, and a limitless strict key is what the edge may decide
without a key hold (docs/design/fast-admission-e2-strict-keys.md). That rests
on a runtime chokepoint, one guarded function per backend that refuses the
change in the writing transaction:

- Spanner: storage_gcp_key_limit_mirror.write_key_limit_rows
- Postgres: PostgresStore._write_key_limit_caps_tx

tests/key_limit_writers.py finds every place that names tr_key_limit (in any
case, quoting, string construction, constant or variable) or writes a table it
cannot resolve, in Python, shell, SQL, Terraform, YAML and the fast path's Go.
Each is listed below with why it cannot break the rule; a new one fails until
it is reviewed and listed, and a listed one that is gone fails too. The scopes
that may write a limit column must be exactly the chokepoints and the
documented exceptions, each pinned by its own test.
"""

from __future__ import annotations

import functools
import textwrap
from pathlib import Path

import pytest

from tests.key_limit_writers import (
    ROOT,
    limit_column_writers,
    repository_paths,
    scan_paths,
    scan_paths_detail,
    scan_repository_detail,
)


@functools.cache
def _detail() -> dict[tuple[str, str, str], bool]:
    return scan_repository_detail()


_READ = "reads the table (a read call, or a read of the in-memory fake), never writes it"
_COUNTER_DML = "assigns usage, reserved or window usage only; never a limit column"
_DDL = "the table's schema (DDL) only"
_FIXTURE = "edits the in-memory fake's rows to build golden vectors or a load test, never a database"

CHOKEPOINTS = {
    ("src/trusted_router/storage_gcp_key_limit_mirror.py", "write_key_limit_rows"),
    ("src/trusted_router/storage_postgres.py", "PostgresStore._write_key_limit_caps_tx"),
}

#: Scopes outside the chokepoints that write a limit column, each with the test
#: that pins why it cannot give a limitless strict key a limit.
LIMIT_COLUMN_EXCEPTIONS = {
    ("scripts/expand_billing_shards.py", "mutations"): (
        "an operator REST script (no Python Spanner client to route through the chokepoint) that inserts NEW "
        "shard rows, never overwriting a row, with the entity's window caps; make_plan refuses a strict key "
        "first (tests/test_strict_key_first_limit.py::test_shard_expansion_refuses_a_strict_key)"
    ),
    ("scripts/backfill_leased_key_usage.py", "update_sql"): (
        "its SET list is built at run time from usage and window usage only "
        "(tests/test_strict_key_first_limit.py::test_the_leased_usage_backfill_assigns_no_limit_column)"
    ),
}

KEY_LIMIT_WRITERS: dict[tuple[str, str, str], str] = {
    # The chokepoints.
    ("key_limit.name", "src/trusted_router/storage_gcp_key_limit_mirror.py", "write_key_limit_rows"): (
        "the Spanner chokepoint"
    ),
    ("key_limit.sql", "src/trusted_router/storage_postgres.py", "PostgresStore._write_key_limit_caps_tx"): (
        "the Postgres chokepoint"
    ),
    ("key_limit.name", "src/trusted_router/storage_gcp_counters.py", "<module>.KEY_LIMIT_TABLE"): (
        "defines the table name; every scope that uses it is a finding of its own"
    ),
    # Counter DML: no limit column assigned.
    ("key_limit.sql", "src/trusted_router/storage_gcp_counter_dml.py", "reserve_key_statement"): _COUNTER_DML,
    ("key_limit.sql", "src/trusted_router/storage_gcp_counter_dml.py", "release_key_statement"): _COUNTER_DML,
    ("key_limit.sql", "src/trusted_router/storage_gcp_strict_budget.py", "reserve_strict_key"): _COUNTER_DML,
    ("key_limit.sql", "src/trusted_router/storage_postgres.py", "PostgresStore._release_key_hold_tx"): _COUNTER_DML,
    ("key_limit.sql", "src/trusted_router/storage_postgres.py",
     "PostgresStore.finalize_gateway_authorization.finalize"): _COUNTER_DML,
    ("key_limit.sql", "src/trusted_router/storage_postgres.py", "PostgresStore.reserve_key_limit.reserve"): (
        _COUNTER_DML
    ),
    ("key_limit.name", "src/trusted_router/storage_gcp_counter_reconcile.py", "repair_typed_reserved._txn"): (
        "repairs reserved only (its columns are key_hash, shard, reserved, updated_at)"
    ),
    ("key_limit.sql", "scripts/backfill_leased_key_usage.py", "update_sql"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[("scripts/backfill_leased_key_usage.py",
                                                                 "update_sql")]
    ),
    # Row deletes: a deleted row has no limit to gain.
    ("key_limit.sql", "src/trusted_router/storage_postgres.py", "PostgresStore._delete_keys_batch.delete"): (
        "deletes a deleted key's row (Postgres)"
    ),
    ("key_limit.name", "src/trusted_router/storage_gcp_key_shard_admin.py", "reshard_key_usage.txn"): (
        "deletes shards past a shrunk count; its limit columns go through the chokepoint, and a strict key is "
        "refused before anything is written"
    ),
    ("key_limit.name", "scripts/cleanup_smoke_signups.py", "main.txn"): (
        "deletes synthetic smoke signups whole, key and rows together"
    ),
    # Operator scripts.
    ("key_limit.name", "scripts/expand_billing_shards.py", "mutations"): (
        "a limit-column exception: " + LIMIT_COLUMN_EXCEPTIONS[("scripts/expand_billing_shards.py", "mutations")]
    ),
    ("key_limit.name", "scripts/expand_billing_shards.py", "Reader.plan"): _READ,
    # Fixtures and load tests (the in-memory fake only).
    ("key_limit.name", "scripts/fastpath/authorize_vectors.py", "build_world"): _FIXTURE,
    ("key_limit.name", "scripts/fastpath/authorize_vectors.py", "stored"): _READ,
    ("key_limit.name", "scripts/stress_credit_shards.py", "run_stress"): _READ,
    # Other files.
    ("key_limit.text", "fastpath/internal/store/gatewaykey.go", "<file>"): (
        "the edge's read of a strict key's shard 0 (ReadRowWithOptions)"
    ),
    ("key_limit.text", "fastpath/internal/store/keywindows.go", "<file>"): (
        "the edge's read of a key's window rows (ReadWithOptions)"
    ),
    ("key_limit.text", "fastpath/schema/fastpath.sql", "<file>"): _DDL,
    ("key_limit.text", "src/trusted_router/storage_postgres_schema.sql", "<file>"): _DDL,
    ("key_limit.text", "scripts/deploy/migrate_typed_counters.sh", "<file>"): _DDL,
    ("key_limit.text", "infra/key_usage_alerts.tf", "<file>"): "an alert's documentation text",
    # Mutations and DML whose table is computed: none is tr_key_limit.
    ("mutation.unresolved", "src/trusted_router/fastpath_enabler.py", "_apply"): (
        "applies the enabler's own fastpath table mutations (tr_fastpath_workspace)"
    ),
    ("mutation.unresolved", "src/trusted_router/storage_gcp_speculation_shadow.py", "Transaction.flush"): (
        "the speculation shadow's own tables (table_name), never tr_key_limit"
    ),
    ("mutation.unresolved", "fastpath/internal/switchsql/switchsql.go", "<file>"): (
        "applies the fast path switch's own table mutations"
    ),
    ("sql.dynamic", "src/trusted_router/analytics_sink.py", "ClickHouseAnalyticsSink._post_rows"): (
        "ClickHouse analytics tables"
    ),
    ("sql.dynamic", "src/trusted_router/operational_analytics_direct.py", "DirectOperationalAnalyticsSink.flush"): (
        "ClickHouse analytics tables"
    ),
    ("sql.dynamic", "src/trusted_router/storage_gcp_settle_outbox.py", "done_retention_statements"): (
        "settle outbox retention"
    ),
    ("sql.dynamic", "clickhouse/ingest_operational_outbox.py", "ClickHouseOperationalWriter.insert"): (
        "ClickHouse tables"
    ),
    ("sql.dynamic", "clickhouse/ingest_outbox.py", "ClickHouseWriter.insert"): "ClickHouse tables",
    ("sql.dynamic", "clickhouse/refresh_workspace_directory.py", "ClickHouseDirectoryWriter._insert"): (
        "ClickHouse tables"
    ),
    ("sql.dynamic", "clickhouse/rollup_analytics.py", "recompute_partition"): "ClickHouse tables",
    ("sql.dynamic", "scripts/deploy/clickhouse_capacity_smoke.sh", "<file>"): "ClickHouse tables",
    ("sql.dynamic", "scripts/deploy/clickhouse_replicate_rollups.sh", "<file>"): "ClickHouse tables",
}


def test_every_writer_of_tr_key_limit_is_known() -> None:
    unknown = sorted(_detail().keys() - KEY_LIMIT_WRITERS.keys())
    assert not unknown, (
        "New code that names tr_key_limit, or writes a table the scanner cannot resolve. A limit column is "
        "written only through storage_gcp_key_limit_mirror.write_key_limit_rows (Spanner) or "
        "PostgresStore._write_key_limit_caps_tx (Postgres); list each finding here with why it cannot give a "
        f"limitless strict key a limit: {unknown}"
    )


def test_the_writer_inventory_has_no_dead_entries() -> None:
    dead = sorted(KEY_LIMIT_WRITERS.keys() - _detail().keys())
    assert not dead, f"Listed writers no longer found; remove them: {dead}"


def test_only_the_chokepoints_write_a_limit_column() -> None:
    writers = limit_column_writers(_detail())
    assert writers - CHOKEPOINTS - LIMIT_COLUMN_EXCEPTIONS.keys() == set(), (
        "A limit column of tr_key_limit is written outside the guarded functions; route it through "
        "write_key_limit_rows (Spanner) or _write_key_limit_caps_tx (Postgres)"
    )
    assert writers >= CHOKEPOINTS, "the scanner no longer sees a chokepoint write its limit columns"
    assert LIMIT_COLUMN_EXCEPTIONS.keys() <= writers, "a listed exception no longer writes a limit column"


def test_the_scan_covers_python_shell_sql_and_go() -> None:
    python, text = repository_paths()
    assert any(p.name == "storage_gcp_key_limit_mirror.py" for p in python)
    assert any(p.parts[-2:] == ("clickhouse", "ingest_outbox.py") for p in python)
    assert not any("tests" in p.relative_to(ROOT).parts for p in python + text)
    assert any(p.suffix == ".sh" for p in text) and any(p.suffix == ".sql" for p in text)
    assert any(p.suffix == ".go" for p in text) and not any(p.name.endswith("_test.go") for p in text)


def _scan_sample(tmp_path: Path, name: str, source: str) -> dict[tuple[str, str], bool]:
    path = tmp_path / name
    path.write_text(textwrap.dedent(source))
    detail = scan_paths_detail([path], []) if path.suffix == ".py" else scan_paths_detail([], [path])
    return {(style, where): limit for (style, _path, where), limit in detail.items()}


# The reviewer's forms the previous inventory missed (review round 2), as
# given. Those that name tr_key_limit or compute their table are found; those
# that name only tr_entities are out of this inventory's scope by design: no
# entity write reaches tr_key_limit's limits except through the chokepoints.
REVIEWER_FORMS = {
    "positional.py": (
        "def f(txn, table, rows):\n    txn.insert_or_update(table, ('kind', 'id', 'body'), rows)\n",
        {("mutation.unresolved", "f"): False},
    ),
    "bulk.py": (
        "def f(txn, rows):\n    args = {'table': 'tr_entities', 'columns': ('kind', 'id', 'body')}\n"
        "    args['values'] = rows\n    txn.insert_or_update(**args)\n",
        {},
    ),
    "helper.py": (
        "def f(store, tx, key):\n    emit = store._write_entity_tx\n    kind = 'api_key'\n"
        "    emit(tx, kind, key.hash, key)\n",
        {},
    ),
    "format.py": (
        "def f(txn):\n    table = 'tr_entities'\n"
        "    txn.execute_update('UPDATE %s SET body=@b WHERE kind=@k AND id=@i' % table)\n",
        {},
    ),
    "variable.sh": (
        "table=tr_key_limit\ngcloud spanner databases execute-sql db "
        "--sql=\"UPDATE $table SET limit_micro=1 WHERE key_hash='k' AND shard=0\"\n",
        {("key_limit.text", "<file>"): False, ("sql.dynamic", "<file>"): True},
    ),
    "quoted.sql": (
        "UPDATE `tr_key_limit` SET limit_micro=1 WHERE key_hash='k' AND shard=0;\n",
        {("key_limit.text", "<file>"): True},
    ),
    "variable.go": (
        'package example\nimport "cloud.google.com/go/spanner"\nfunc f() { table := "tr_key_limit"; '
        '_ = spanner.InsertOrUpdate(table, []string{"key_hash", "shard", "limit_micro"}, '
        '[]interface{}{"k", 0, 1}) }\n',
        {("key_limit.text", "<file>"): True, ("mutation.unresolved", "<file>"): True},
    ),
}


@pytest.mark.parametrize("name", REVIEWER_FORMS)
def test_the_reviewers_forms(tmp_path: Path, name: str) -> None:
    source, expected = REVIEWER_FORMS[name]
    assert _scan_sample(tmp_path, name, source) == expected


# The same constructions aimed at tr_key_limit, and the other ways to name it.
KEY_LIMIT_FORMS = [
    ("positional computed table, limit columns",
     "def f(txn, table, rows):\n    txn.update(table, ('key_hash', 'shard', 'limit_micro'), rows)\n",
     {("mutation.unresolved", "f"): True}),
    ("bulk **kwargs",
     "def f(txn, rows):\n    args = {'table': 'tr_key_limit', 'columns': ('key_hash', 'shard', 'day_limit_micro')}\n"
     "    args['values'] = rows\n    txn.insert_or_update(**args)\n",
     {("key_limit.name", "f"): True}),
    ("bulk **kwargs, table added after",
     "def f(txn, rows):\n    args = {'columns': ('key_hash', 'shard', 'limit_micro'), 'values': rows}\n"
     "    args['table'] = 'TR_KEY_LIMIT'\n    txn.update(**args)\n",
     {("key_limit.name", "f"): True}),
    ("bulk **kwargs, not known",
     "def f(txn, args):\n    txn.insert_or_update(**args)\n", {("mutation.unresolved", "f"): True}),
    ("aliased mutation",
     "from trusted_router.storage_gcp_counters import KEY_LIMIT_TABLE\n"
     "def f(txn, rows):\n    emit = txn.insert_or_update\n    emit(table=KEY_LIMIT_TABLE, columns=('key_hash', 'shard',"
     " 'week_limit_micro'), values=rows)\nKEY_LIMIT_TABLE = 'tr_key_limit'\n",
     {("key_limit.name", "f"): True, ("key_limit.name", "<module>.KEY_LIMIT_TABLE"): False}),
    ("aliased positional mutation, table not known",
     "def f(txn, t, rows):\n    emit = txn.insert_or_update\n    emit(t, ('key_hash', 'shard', 'limit_micro'), rows)\n",
     {("mutation.unresolved", "f"): True}),
    ("dynamic dispatch",
     "def f(txn, verb, rows):\n    getattr(txn, verb)(table='tr_key_limit', columns=COLS, values=rows)\n"
     "COLS = ('key_hash', 'shard', 'month_limit_micro')\n",
     {("key_limit.name", "f"): True}),
    ("%-formatted SQL",
     "def f(txn):\n    table = 'tr_key_limit'\n"
     "    txn.execute_update('UPDATE %s SET limit_micro=1 WHERE key_hash=@k AND shard=0' % table)\n",
     {("key_limit.name", "f"): False, ("key_limit.sql", "f"): True}),
    ("%-formatted SQL, table not known",
     "def f(txn, table):\n    txn.execute_update('UPDATE %s SET limit_micro=1 WHERE key_hash=@k' % table)\n",
     {("sql.dynamic", "f"): True}),
    ("format() SQL",
     "T = 'tr_key_limit'\ndef f(txn):\n    txn.execute_update('UPDATE {} SET day_limit_micro=1'.format(T))\n",
     {("key_limit.name", "<module>.T"): False, ("key_limit.sql", "f"): True}),
    ("concatenated table name",
     "def f(txn):\n    txn.execute_update('UPDATE tr_' + 'key_limit SET limit_micro=1 WHERE key_hash=@k')\n",
     {("key_limit.sql", "f"): True}),
    ("concatenated SQL, table not known",
     "def f(txn, t):\n    txn.execute_update('UPDATE ' + t + ' SET reserved=0')\n", {("sql.dynamic", "f"): True}),
    ("SQL head, table appended later",
     "def f(txn, t):\n    sql = 'DELETE FROM '\n    sql += t\n    txn.execute_update(sql)\n",
     {("sql.dynamic", "f"): True}),
    ("backtick-quoted, lowercase, lock hint",
     "def f(t):\n    t.execute_update('@{LOCK_SCANNED_RANGES=EXCLUSIVE} update `tr_key_limit` set limit_micro = 1')\n",
     {("key_limit.sql", "f"): True}),
    ("f-string table from a constant",
     "def f(t):\n    t.execute_update(f'UPDATE {KL} SET reserved = 0 WHERE key_hash = @k')\nKL = 'tr_key_limit'\n",
     {("key_limit.sql", "f"): False, ("key_limit.name", "<module>.KL"): False}),
    ("f-string column filled in",
     "def f(t, column):\n    t.execute_update(f'UPDATE tr_key_limit SET {column} = 1')\n",
     {("key_limit.sql", "f"): True}),
    ("f-string column from a known choice",
     "def f(t, byok):\n    column = 'byok_usage' if byok else 'usage'\n"
     "    t.execute_update(f'UPDATE tr_key_limit SET {column} = {column} + @a')\n",
     {("key_limit.sql", "f"): False}),
    ("INSERT columns, then an upsert",
     "def f(c):\n    c.execute('INSERT INTO tr_key_limit (workspace_id, key_hash, shard, month_limit_micro) "
     "VALUES (%s, %s, 0, %s) ON CONFLICT (workspace_id, key_hash, shard) DO NOTHING')\n",
     {("key_limit.sql", "f"): True}),
    ("positional delete",
     "def f(txn, keyset):\n    txn.delete('tr_key_limit', keyset)\n", {("key_limit.name", "f"): False}),
    ("REST payload",
     "def f(rows):\n    return [{'insert': {'table': 'tr_key_limit', 'columns': ['key_hash', *CAPS], 'values': rows}}]\n"
     "CAPS = {'day_limit_micro': 'limit_daily_microdollars'}\n",
     {("key_limit.name", "f"): True}),
    ("the create batch before the chokepoint",
     "def create(batch, rows):\n    batch.insert_or_update(table=KEY_LIMIT_TABLE, columns=KEY_LIMIT_COLUMNS, values=rows)\n"
     "KEY_LIMIT_TABLE = 'tr_key_limit'\nKEY_LIMIT_COLUMNS = ('key_hash', 'shard', 'limit_micro', 'include_byok')\n",
     {("key_limit.name", "create"): True, ("key_limit.name", "<module>.KEY_LIMIT_TABLE"): False}),
    ("the escrow update before the chokepoint",
     "def txn(t, changed):\n    t.update(table=KEY_LIMIT_TABLE, columns=('key_hash', 'shard', 'limit_micro'), "
     "values=changed)\nKEY_LIMIT_TABLE = 'tr_key_limit'\n",
     {("key_limit.name", "txn"): True, ("key_limit.name", "<module>.KEY_LIMIT_TABLE"): False}),
]


@pytest.mark.parametrize(("name", "source", "expected"), KEY_LIMIT_FORMS, ids=[c[0] for c in KEY_LIMIT_FORMS])
def test_each_way_to_write_tr_key_limit_is_found(tmp_path: Path, name: str, source: str, expected: dict) -> None:
    assert _scan_sample(tmp_path, "sample.py", source) == expected, name


NOT_WRITES = [
    ("a SELECT", "def f(t):\n    return t.execute_sql('SELECT limit_micro FROM tr_key_limit WHERE key_hash=@k')\n"),
    ("a SELECT ... FOR UPDATE",
     "def f(c):\n    c.execute('SELECT limit_micro FROM tr_key_limit WHERE key_hash = %s FOR UPDATE')\n"),
    ("a SELECT built from a constant",
     "def f(t):\n    return t.execute_sql('SELECT 1 FROM ' + T + ' WHERE key_hash=@k')\nT = 'tr_credit_balance'\n"),
    ("a message", "def f():\n    raise RuntimeError('configured tr_key_limit usage shard set is incomplete')\n"),
    ("a docstring", "def f():\n    '''Updates nothing in tr_key_limit.'''\n"),
    ("an index name", "def f(t):\n    return t.execute_sql('SELECT 1 FROM x@{FORCE_INDEX=tr_key_limit_by_key_hash}')\n"),
    ("str.replace", "def f(s, a, b):\n    return s.replace(a, b, 1)\n"),
    ("dict.update with **", "def f(d, **fields):\n    d.update(scope=1, **fields)\n"),
    ("a read request", "def f(c, t):\n    return c.read(request={'table': t, 'columns': ['x'], 'key_set': {}})\n"),
    ("another table's mutation",
     "def f(txn, rows):\n    txn.insert_or_update(table='tr_credit_balance', columns=('workspace_id',), values=rows)\n"),
    ("a log line", "def f(log, n):\n    log.info('could not update %s keys', n)\n"),
]


@pytest.mark.parametrize(("name", "source"), NOT_WRITES, ids=[c[0] for c in NOT_WRITES])
def test_reads_and_mentions_are_not_writes(tmp_path: Path, name: str, source: str) -> None:
    assert _scan_sample(tmp_path, "sample.py", source) == {}, name


@pytest.mark.parametrize(("name", "suffix", "source", "expected"), [
    ("shell DML", ".sh", "gcloud spanner databases execute-sql db --sql=\"UPDATE TR_KEY_LIMIT SET reserved=0\"\n",
     {("key_limit.text", "<file>"): False}),
    ("SQL file delete", ".sql", "delete from tr_key_limit where key_hash = 'k';\n",
     {("key_limit.text", "<file>"): False}),
    ("SQL upsert of a limit", ".sql", "INSERT OR UPDATE tr_key_limit (key_hash, shard, day_limit_micro) "
     "VALUES ('k', 0, 1);\n", {("key_limit.text", "<file>"): True}),
    ("Go literal mutation", ".go", 'm := spanner.InsertOrUpdate("tr_key_limit", []string{"limit_micro"}, vals)\n',
     {("key_limit.text", "<file>"): True}),
    ("Go format", ".go", 'q := fmt.Sprintf("UPDATE %s SET reserved = 0", table)\n', {("sql.dynamic", "<file>"): True}),
    ("YAML job", ".yaml", "args: [\"--sql=UPDATE tr_key_limit SET week_limit_micro = 1\"]\n",
     {("key_limit.text", "<file>"): True}),
    ("a comment only", ".sh", "# tr_key_limit is created by migrate_typed_counters.sh\n", {}),
])
def test_each_text_form_is_found(tmp_path: Path, name: str, suffix: str, source: str, expected: dict) -> None:
    assert _scan_sample(tmp_path, "sample" + suffix, source) == expected, name


def test_the_scanner_finds_every_writer_the_chokepoints_replaced(tmp_path: Path) -> None:
    """Before the chokepoints, key creation, the limit edit, the reshard and
    the escrow each wrote the limit columns themselves; scanned as they were,
    each is a limit-column writer outside the chokepoints."""
    before = '''
    from trusted_router.storage_gcp_counters import KEY_LIMIT_COLUMNS, KEY_LIMIT_TABLE

    class SpannerApiKeys:
        def create(self, batch, key, rows):
            batch.insert_or_update(table=KEY_LIMIT_TABLE, columns=KEY_LIMIT_COLUMNS, values=rows)

        def update(self, transaction, config_rows, counters):
            transaction.insert_or_update(
                table=KEY_LIMIT_TABLE,
                columns=KEY_LIMIT_COLUMNS + KEY_LIMIT_COUNTER_COLUMNS,
                values=[config + counters[int(config[1])] for config in config_rows],
            )

    def rebalance(transaction, changed):
        transaction.update(table=KEY_LIMIT_TABLE, columns=("key_hash", "shard", "limit_micro"), values=changed)
    '''
    sample = tmp_path / "before.py"
    sample.write_text(textwrap.dedent(before))
    counters = ROOT / "src" / "trusted_router" / "storage_gcp_counters.py"
    keys = ROOT / "src" / "trusted_router" / "storage_gcp_keys.py"
    detail = scan_paths_detail([sample, counters, keys], [])
    assert {where for path, where in limit_column_writers(detail) if path == "before.py"} == {
        "SpannerApiKeys.create", "SpannerApiKeys.update", "rebalance"}


def test_scan_paths_matches_the_detail(tmp_path: Path) -> None:
    sample = tmp_path / "s.py"
    sample.write_text("def f(t):\n    t.delete('tr_key_limit', ks)\n")
    assert scan_paths([sample], []) == set(scan_paths_detail([sample], []))


def test_the_root_is_the_repository() -> None:
    assert (ROOT / "src" / "trusted_router" / "storage_key_patch.py").is_file()


# Review round 3: writes of a limit column the previous column check missed,
# each a positive control, with the forms that write none beside them.
COLUMN_FORMS = [
    ("tuple assignment", "UPDATE tr_key_limit SET (reserved, day_limit_micro) = (GREATEST(reserved - %s, 0), 5000000)"
     " WHERE key_hash = %s", True),
    ("tuple assignment, no limit", "UPDATE tr_key_limit SET (reserved, usage) = (0, 0) WHERE key_hash = %s", False),
    ("schema-qualified insert", "INSERT INTO public.tr_key_limit (workspace_id, key_hash, shard, limit_micro) "
     "VALUES (%s, %s, 0, 10)", True),
    ("quoted schema-qualified update", 'UPDATE "public"."tr_key_limit" SET "week_limit_micro" = 1', True),
    ("schema-qualified counter update", "UPDATE public.tr_key_limit SET reserved = 0 WHERE key_hash = %s", False),
    ("insert without a column list, from a query", "INSERT INTO tr_key_limit SELECT * FROM restored_key_limits", True),
    ("insert without a column list, values", "INSERT INTO tr_key_limit VALUES (%s, %s, 0, 10)", True),
    ("qualified target through an alias", "UPDATE tr_key_limit AS k SET k.day_limit_micro = 1 WHERE k.key_hash = %s",
     True),
    ("a limit after a subquery", "UPDATE tr_key_limit SET reserved = (SELECT 0 FROM x), limit_micro = 5", True),
    ("a block comment before the table", "UPDATE /* repair */ tr_key_limit SET month_limit_micro = 1", True),
    ("upsert's update", "INSERT INTO tr_key_limit (key_hash, shard) VALUES (%s, 0) ON CONFLICT (workspace_id, "
     "key_hash, shard) DO UPDATE SET limit_micro = 1", True),
    ("upsert that does nothing", "INSERT INTO tr_key_limit (key_hash, shard) VALUES (%s, 0) ON CONFLICT "
     "(workspace_id, key_hash, shard) DO NOTHING", False),
    ("a target filled in", "UPDATE tr_key_limit SET {} WHERE key_hash = %s", True),
    ("an expression as a target", "UPDATE tr_key_limit SET coalesce(x) = 1", True),
    ("DML and the table, no statement on it read", "UPDATE tr_key_limit_shadow SET a = 1; SELECT 1 FROM tr_key_limit",
     True),
    ("a delete", "DELETE FROM tr_key_limit WHERE key_hash = %s", False),
    ("a merge", "MERGE INTO tr_key_limit USING x ON y WHEN MATCHED THEN UPDATE SET reserved = 0", True),
]


@pytest.mark.parametrize(("name", "sql", "writes_limit"), COLUMN_FORMS, ids=[c[0] for c in COLUMN_FORMS])
def test_each_way_to_write_a_limit_column_is_found(tmp_path: Path, name: str, sql: str, writes_limit: bool) -> None:
    from tests.key_limit_writers import assigns_limit_column

    assert assigns_limit_column(sql) is writes_limit, name
    source = f"def f(conn):\n    conn.execute({sql!r})\n"
    assert _scan_sample(tmp_path, "sample.py", source) == {("key_limit.sql", "f"): writes_limit}, name
    assert _scan_sample(tmp_path, "sample.sql", sql + ";\n") == {("key_limit.text", "<file>"): writes_limit}, name


def test_the_reviewers_injected_cap_write_is_caught(tmp_path: Path) -> None:
    """The reviewer's probe: a tuple assignment of a limit column injected
    into a listed counter writer (PostgresStore._release_key_hold_tx). The
    column check now names that scope a limit-column writer outside the
    chokepoints."""
    source = (ROOT / "src/trusted_router/storage_postgres.py").read_text()
    old = '"   reserved = GREATEST(reserved - %s, 0)"'
    assert source.count(old) == 1
    injected = tmp_path / "storage_postgres.py"
    injected.write_text(source.replace(old, '"   (reserved, day_limit_micro) = (GREATEST(reserved - %s, 0), 5000000)"'))
    writers = limit_column_writers(scan_paths_detail([injected], []))
    assert ("storage_postgres.py", "PostgresStore._release_key_hold_tx") in writers
    clean = tmp_path / "clean" / "storage_postgres.py"
    clean.parent.mkdir()
    clean.write_text(source)
    assert ("storage_postgres.py", "PostgresStore._release_key_hold_tx") not in limit_column_writers(
        scan_paths_detail([clean], []))
