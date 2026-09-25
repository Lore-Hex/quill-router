"""Executable controls for the guard's order, instrumentation, and scope."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import psycopg
import pytest

from tests.fakes import lock_order
from tests.fakes.postgres import SqlitePostgresConn, postgres_store_on, sqlite_postgres_conn
from tests.fakes.spanner import FakeSpannerDatabase, _FakeTransaction, _KeySet

CREDIT = "UPDATE tr_credit_balance SET reserved = reserved + 1 WHERE workspace_id = 'w'"
KEY = "UPDATE tr_key_limit SET reserved = reserved + 1 WHERE key_hash = 'k'"


def _pg_store():
    conn = sqlite_postgres_conn(check_same_thread=False)
    conn.execute("INSERT INTO tr_credit_balance (workspace_id, total_credits) VALUES ('w', 100)")
    conn.execute("INSERT INTO tr_key_limit (workspace_id, key_hash) VALUES ('w', 'k')")
    return postgres_store_on(conn), conn


def _execute(conn, sql):
    assert conn.execute(sql).rowcount == 1


def _spanner_db():
    db = FakeSpannerDatabase()
    db.typed["tr_credit_balance"] = {
        ("w", 0): {"workspace_id": "w", "shard": 0, "total_credits": 100,
                   "total_usage": 0, "reserved": 0},
    }
    db.typed["tr_key_limit"] = {
        ("k", 0): {"key_hash": "k", "shard": 0, "reserved": 0},
    }
    return db


def _spanner_credit(tx):
    assert tx.execute_update(
        "UPDATE tr_credit_balance SET reserved = reserved + @est "
        "WHERE workspace_id=@ws AND shard=@shard",
        params={"ws": "w", "shard": 0, "est": 1},
    ) == 1


def _spanner_key(tx):
    assert tx.execute_sql(
        "SELECT reserved FROM tr_key_limit WHERE key_hash=@kh", params={"kh": "k"},
    ) == [[0]]


@pytest.mark.parametrize("order", ["kc", "kck", "ckc", "ckck", "b"])
def test_detector_rejects_inversions_and_simultaneous_locks(order):
    record = lock_order._Recorder()
    for kind in order:
        record.record("t", {"c": CREDIT, "k": KEY, "b": (
            "SELECT * FROM tr_credit_balance c JOIN tr_key_limit k ON true FOR UPDATE"
        )}[kind])
    assert len(record.violations()) == 1
    with pytest.raises(lock_order.LockOrderError, match="deadlock shape"):
        record.check("control")


def test_non_vacuity_and_unique_transaction_count_reset():
    """One seeded transaction really updates both classes; runnable alone."""
    store, conn = _pg_store()
    lock_order.recorder.reset()
    store._run_transaction(lambda c: (_execute(c, CREDIT), _execute(c, KEY)))
    assert conn.balance("w") == (100, 0, 1)
    assert conn.execute("SELECT reserved FROM tr_key_limit").fetchone() == (1,)
    assert len(lock_order.recorder._tx) == 1
    for _ in range(3):
        assert lock_order.recorder.violations() == []
        lock_order.recorder.check("non-vacuity")
        assert lock_order.recorder.both_tables_seen == 1
    lock_order.recorder.reset()
    assert lock_order.recorder.both_tables_seen == 0


@pytest.mark.parametrize("backend", ["postgres", "spanner"])
@pytest.mark.parametrize("inverted", [False, True])
def test_hooks_execute_seeded_statements(backend, inverted):
    lock_order.recorder.reset()
    if backend == "postgres":
        store, conn = _pg_store()
        statements = (KEY, CREDIT) if inverted else (CREDIT, KEY)
        store._run_transaction(lambda c: [_execute(c, sql) for sql in statements])
        assert conn.balance("w") == (100, 0, 1)
        assert conn.execute("SELECT reserved FROM tr_key_limit").fetchone() == (1,)
    else:
        db = _spanner_db()
        statements = (_spanner_key, _spanner_credit) if inverted else (_spanner_credit, _spanner_key)
        db.run_in_transaction(lambda tx: [statement(tx) for statement in statements])
        assert db.typed["tr_credit_balance"][("w", 0)]["reserved"] == 1
        assert db.transaction_execute_sql_calls == db.transaction_execute_update_calls == 1
    assert lock_order.recorder.both_tables_seen == 1
    if inverted:
        with pytest.raises(lock_order.LockOrderError):
            lock_order.recorder.check("hook-control")
    else:
        lock_order.recorder.check("hook-clean")
    lock_order.recorder.reset()


# The same normalizer feeds both backends. Spanner's implicit read locks are
# intentional; the text 'for update' alone must not turn on Postgres locking.
@pytest.mark.parametrize("read_locks", [False, True], ids=["postgres", "spanner"])
@pytest.mark.parametrize(("sql", "expected_pg", "expected_spanner"), [
    (CREDIT + " /* tr_key_limit */", {"credit"}, {"credit"}),
    (KEY + " /* tr_credit_balance */", {"key"}, {"key"}),
    (CREDIT + " -- tr_key_limit\n", {"credit"}, {"credit"}),
    (KEY + " -- tr_credit_balance\n", {"key"}, {"key"}),
    ("UPDATE unrelated SET pause_epoch=1, billing_pause=1", set(), set()),
    ("SELECT pause_epoch, billing_pause FROM unrelated FOR UPDATE", set(), set()),
    ("SELECT 'tr_credit_balance', 'tr_key_limit' FROM unrelated FOR UPDATE", set(), set()),
    ("SELECT 'it''s tr_credit_balance -- x' FROM unrelated FOR UPDATE", set(), set()),
    ("SELECT 1 FROM unrelated /* tr_credit_balance tr_key_limit */ FOR UPDATE", set(), set()),
    ("SELECT 'for update' FROM tr_credit_balance", set(), {"credit"}),
    ("SELECT 'for update' FROM tr_key_limit", set(), {"key"}),
    ("SELECT 1 FROM tr_credit_balance /* FOR UPDATE */", set(), {"credit"}),
    ("UPDATE tr_credit_balance SET reserved=1 WHERE EXISTS (SELECT 1 FROM tr_key_limit)",
     {"credit"}, {"credit", "key"}),
    ("UPDATE tr_key_limit SET reserved=1 WHERE EXISTS (SELECT 1 FROM tr_credit_balance)",
     {"key"}, {"credit", "key"}),
    ("/* leading */ UPDATE tr_credit_balance SET pause_epoch=1", {"credit"}, {"credit"}),
    ("/* ' */ UPDATE tr_credit_balance SET pause_epoch=1", {"credit"}, {"credit"}),
    ("/* outer /* nested */ tr_key_limit */ UPDATE tr_credit_balance SET pause_epoch=1",
     {"credit"}, {"credit"}),
    ("-- leading\n INSERT INTO tr_key_limit (key_hash) VALUES ('k')", {"key"}, {"key"}),
    ("WITH c AS (SELECT 1) UPDATE tr_credit_balance SET reserved=1", {"credit"}, {"credit"}),
    ("WITH c AS (SELECT 1) DELETE FROM tr_key_limit WHERE shard=0", {"key"}, {"key"}),
    ("WITH c AS (SELECT 1) INSERT INTO tr_credit_balance (workspace_id) VALUES ('w')",
     {"credit"}, {"credit"}),
    ("/* leading */ MERGE INTO tr_credit_balance USING unrelated ON true WHEN MATCHED THEN UPDATE SET reserved=1",
     {"credit"}, {"credit"}),
    ('UPDATE "public"."tr_credit_balance" SET reserved=1', {"credit"}, {"credit"}),
    ("SELECT * FROM tr_trust_event FOR SHARE", {"credit"}, {"credit"}),
    ("SELECT pause_epoch FROM tr_credit_balance FOR NO KEY UPDATE", {"credit"}, {"credit"}),
    ("SELECT * FROM tr_key_limit FOR KEY SHARE", {"key"}, {"key"}),
    ("SELECT * FROM tr_credit_balance c JOIN tr_key_limit k ON true FOR UPDATE",
     {"credit", "key"}, {"credit", "key"}),
    ("SELECT * FROM tr_credit_balance c JOIN tr_key_limit k ON true FOR UPDATE OF c",
     {"credit"}, {"credit", "key"}),
    ("SELECT * FROM tr_credit_balance c JOIN tr_key_limit k ON true FOR UPDATE OF k",
     {"key"}, {"credit", "key"}),
    ("SELECT * FROM tr_credit_balance c JOIN tr_key_limit k ON true FOR UPDATE OF c, k",
     {"credit", "key"}, {"credit", "key"}),
    ("WITH c AS (SELECT * FROM tr_credit_balance) SELECT * FROM c FOR UPDATE",
     {"credit"}, {"credit"}),
    ("WITH c AS (UPDATE tr_credit_balance SET reserved=1 RETURNING *) UPDATE tr_key_limit SET reserved=1",
     {"credit", "key"}, {"credit", "key"}),
    ("UPDATE tr_credit_balance SET reserved=1 WHERE EXISTS (SELECT 1 FROM tr_key_limit FOR UPDATE)",
     {"credit", "key"}, {"credit", "key"}),
    ("UPDATE tr_key_limit SET reserved=1 WHERE EXISTS (SELECT 1 FROM tr_credit_balance FOR UPDATE)",
     {"credit", "key"}, {"credit", "key"}),
])
def test_classification_neither_hides_nor_invents_locks(sql, expected_pg, expected_spanner, read_locks):
    expected = expected_spanner if read_locks else expected_pg
    record = lock_order._Recorder()
    record.record("one", sql, read_locks=read_locks)
    actual = set().union(*(kinds for kinds, _ in record._tx.get("one", [])))
    assert actual == expected
    # Check both possible positions: spurious or hidden classes change whether
    # an inversion is found. A same-statement pair must remain simultaneous.
    for statements, inverted in [
        ([KEY, sql], "credit" in expected),
        ([sql, CREDIT], "key" in expected),
    ]:
        record.reset()
        for statement in statements:
            record.record("t", statement, read_locks=read_locks)
        assert bool(record.violations()) is inverted


@pytest.mark.parametrize("inverted_first", [False, True])
def test_retry_attempts_do_not_merge_or_hide_inversions(inverted_first):
    store, conn = _pg_store()
    lock_order.recorder.reset()
    attempts = 0

    def work(c):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            _execute(c, KEY)
            if inverted_first:
                _execute(c, CREDIT)
            raise psycopg.errors.SerializationFailure("retry control")
        _execute(c, KEY if inverted_first else CREDIT)

    store._run_transaction(work)
    assert attempts == 2
    traces = list(lock_order.recorder._tx.values())
    assert [len(trace) for trace in traces] == ([2, 1] if inverted_first else [1, 1])
    assert conn.balance("w") == (100, 0, 0 if inverted_first else 1)
    if inverted_first:
        assert len(lock_order.recorder.violations()) == 1
        with pytest.raises(lock_order.LockOrderError):
            lock_order.recorder.check("retry-inversion")
    else:
        lock_order.recorder.check("rollback-not-inversion")
    lock_order.recorder.reset()


def test_savepoint_does_not_start_a_new_attempt():
    store, _ = _pg_store()
    lock_order.recorder.reset()

    def work(conn):
        _execute(conn, KEY)
        conn.execute("SAVEPOINT inner_work")
        _execute(conn, CREDIT)
        conn.execute("ROLLBACK TO SAVEPOINT inner_work")
        conn.execute("RELEASE SAVEPOINT inner_work")
        _execute(conn, KEY)

    store._run_transaction(work)
    assert [len(steps) for steps in lock_order.recorder._tx.values()] == [3]
    with pytest.raises(lock_order.LockOrderError):
        lock_order.recorder.check("savepoint-control")
    lock_order.recorder.reset()


def test_overlapping_connections_keep_complete_separate_traces():
    first, _ = _pg_store()
    second, _ = _pg_store()
    first_started, second_started, first_finished = Event(), Event(), Event()
    lock_order.recorder.reset()

    def run_first():
        def work(conn):
            _execute(conn, CREDIT)
            first_started.set()
            assert second_started.wait(5)
            _execute(conn, KEY)
        first._run_transaction(work)
        first_finished.set()

    def run_second():
        assert first_started.wait(5)

        def work(conn):
            _execute(conn, CREDIT)
            second_started.set()
            assert first_finished.wait(5)
            _execute(conn, KEY)
        second._run_transaction(work)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_first), pool.submit(run_second)]
        for future in futures:
            future.result(timeout=10)
    traces = list(lock_order.recorder._tx.values())
    assert len(traces) == 2
    assert [[kinds for kinds, _ in steps] for steps in traces] == [
        [frozenset({"credit"}), frozenset({"key"})],
        [frozenset({"credit"}), frozenset({"key"})],
    ]
    assert lock_order.recorder.both_tables_seen == 2
    lock_order.recorder.check("concurrent-clean")


def test_connection_current_key_is_thread_local():
    _, conn = _pg_store()
    local = lock_order._connection_state(conn)
    local.key = "main-thread"
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(lambda: getattr(lock_order._connection_state(conn), "key", None)).result() is None
    assert local.key == "main-thread"
    local.key = None


@pytest.mark.parametrize("table", ["tr_credit_balance", "tr_key_limit"])
@pytest.mark.parametrize("method", ["insert_or_update", "delete"])
def test_buffered_counter_mutations_are_recorded_as_unproved(table, method):
    db = _spanner_db()
    lock_order.recorder.reset()
    pk = ("w" if table == "tr_credit_balance" else "k", 0)
    row = db.typed[table][pk]

    def work(tx):
        if method == "delete":
            tx.delete(table, _KeySet(keys=[pk]))
        else:
            tx.insert_or_update(table=table, columns=tuple(row), values=[tuple(row.values())])

    db.run_in_transaction(work)
    assert db.commits == 1
    assert (pk in db.typed[table]) is (method == "insert_or_update")
    # A buffered counter write is applied atomically at commit, so its call order
    # takes no lock: this guard cannot order the transaction and must not pretend
    # to. It is UNPROVED, not violating -- failing here would reject the
    # administrative repair paths (repair_typed_reserved and friends) that
    # spanner_order.credit_before_key documents as outside the invariant.
    lock_order.recorder.check("buffered-control")
    assert lock_order.recorder.unproved, (
        "a buffered counter write must be reported as unproved, not silently ordered"
    )
    lock_order.recorder.reset()
    assert not lock_order.recorder.unproved


def test_non_counter_buffered_mutations_are_not_counter_locks():
    tx = _FakeTransaction(_spanner_db())
    tx.insert_or_update(table="tr_entities", columns=("kind", "id", "body"), values=[("x", "y", "{}")])
    assert tx.pending_writes
    assert lock_order.recorder.violations() == []


def test_install_verifies_callable_identity(monkeypatch):
    lock_order.install()
    assert lock_order.recorder.installed
    for owner, name, wrapper in lock_order.recorder.hooks:
        assert getattr(owner, name) is wrapper
        assert wrapper._lock_order_hook
    clean = SqlitePostgresConn.execute.__wrapped__
    with monkeypatch.context() as patch:
        patch.setattr(SqlitePostgresConn, "execute", clean)
        with pytest.raises(RuntimeError, match="SqlitePostgresConn.execute hook replaced"):
            lock_order.install()
    lock_order.install()


def test_known_uncovered_paths_are_documented():
    """Keep the bypass inventory discoverable beside the exact claimed scope."""
    from tests import conftest

    assert "KNOWN_UNCOVERED_PATHS" in lock_order.__doc__
    assert "KNOWN_UNCOVERED_PATHS" in conftest.lock_order_guard.__doc__
    assert lock_order.KNOWN_UNCOVERED_PATHS == (
        "SqlitePostgresConn.transaction() outside PostgresStore._run_transaction",
        "conn._raw.execute",
        "cursors returned by execute",
        "local fake transactions in test_trust_tier_slice1a.py",
        "local fake transactions in test_operational_analytics_outbox_postgres.py",
        "PostgresStore methods taking a connection directly outside the funnel",
        "broader-scoped fixture setup and teardown outside the observation window",
        "real Spanner SDK lazy streaming execution order",
    )


def _inner_pytest(tmp_path: Path, source: str):
    """Run the repository's actual fixture source, including under mutation M8."""
    (tmp_path / "conftest.py").write_text(Path(__file__).with_name("conftest.py").read_text())
    test = tmp_path / f"test_inner_{tmp_path.name}.py"
    test.write_text(source)
    return pytest.main([
        str(test), "-q", "-p", "no:cacheprovider", "--basetemp=" + str(tmp_path / "inner-tmp"),
        "--confcutdir=" + str(tmp_path), "--import-mode=importlib", "-o", "addopts=",
    ])


INNER_HELPERS = '''
import pytest
from tests.test_lock_order_guard import _pg_store, _execute, CREDIT, KEY
from tests.fakes import lock_order

def invert():
    store, _ = _pg_store()
    store._run_transaction(lambda c: (_execute(c, KEY), _execute(c, CREDIT)))
'''


def test_autouse_teardown_enforces_without_explicit_check(tmp_path, capsys):
    result = _inner_pytest(tmp_path, INNER_HELPERS + '''
def test_inversion_without_check():
    invert()
''')
    output = capsys.readouterr().out
    lock_order.recorder.reset()
    assert result == pytest.ExitCode.TESTS_FAILED, output
    assert "ERROR at teardown of test_inversion_without_check" in output
    assert "LockOrderError" in output
    assert "1 passed, 1 error" in output


def test_broader_fixture_setup_and_teardown_are_outside_window(tmp_path, capsys):
    lock_order.install()  # broader setup normally even precedes first install
    result = _inner_pytest(tmp_path, INNER_HELPERS + '''
@pytest.fixture(scope="module", autouse=True)
def broad():
    invert()
    assert lock_order.recorder.violations()
    yield
    invert()

def test_window():
    assert lock_order.recorder._tx == {}  # broad setup was erased by reset
''')
    output = capsys.readouterr().out
    assert result == pytest.ExitCode.OK, output
    assert "1 passed" in output
    # Broad teardown ran after the inner fixture's check and was not enforced.
    with pytest.raises(lock_order.LockOrderError):
        lock_order.recorder.check("outside-window")
    lock_order.recorder.reset()
