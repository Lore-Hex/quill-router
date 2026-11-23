"""Which reserves and releases lock their counter row exclusively at the scan.

docs/incidents/2026-11-10-reserve-lock-upgrade-aborts.md: a conditional UPDATE
scans under a shared lock and upgrades it, so simultaneous writers of one row
abort each other. The statement hint removes the upgrade, but a hinted
statement that matches no row holds the exclusive lock until its transaction
ends. So a reserve carries the hint only where it is expected to write and is
the only counter row of its table the transaction touches. A release always
names the one credit row and the one key row its reservation recorded, so
every release carries it; the folded batch's credit release has a subquery
and so follows a hinted statement that only locks. These tests pin which
statements carry the hint, in which transaction shapes, and that no hinted
statement has a subquery (the hint would lock the subquery's range too).
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.cloud.spanner_v1 import param_types

from tests.fakes import spanner as fake_spanner
from tests.fakes.spanner import _FakeTransaction
from tests.test_authorize_speculative_batch import _options
from tests.test_settle_speculative_batch import fixture as _settle_fixture
from tests.test_settle_speculative_batch import invoke as _settle
from tests.test_spanner_batch_dml import NOW, _database
from trusted_router import storage_gcp_authorize as authorize
from trusted_router import storage_gcp_counter_dml as dml
from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_gcp_operational_analytics_outbox import (
    SpannerOperationalAnalyticsOutbox,
)
from trusted_router.storage_gcp_strict_budget import reserve_strict_key

HINT = "@{LOCK_SCANNED_RANGES=EXCLUSIVE} "
PT = SimpleNamespace(INT64="INT64", STRING="STRING", BOOL="BOOL", TIMESTAMP="TIMESTAMP")
SRC = Path(__file__).resolve().parents[1] / "src" / "trusted_router"
CREDIT = "UPDATE tr_credit_balance SET reserved = reserved + @est "
KEY = "UPDATE tr_key_limit SET reserved = reserved + @est "


class _Recorder:
    """Records the statements a helper issues; every write matches no row."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute_update(self, sql: str, **_: Any) -> int:
        self.statements.append(sql)
        return 0

    def execute_sql(self, sql: str, **_: Any) -> list:
        self.statements.append(sql)
        return []


def _issued(call: Any) -> str:
    recorder = _Recorder()
    call(recorder)
    return recorder.statements[0]


def _strict(is_byok: bool, windows: bool) -> str:
    return _issued(
        lambda tx: reserve_strict_key(tx, PT, "kh", 5, is_byok=is_byok, enforce_windows=windows)
    )


def _hinted() -> dict[str, str]:
    return {
        "reserve_credit": _issued(lambda tx: dml.reserve_credit(tx, PT, "ws", 5, shard=0)),
        "reserve_credit_statement": dml.reserve_credit_statement(PT, "ws", 5, shard=0)[0],
        "reserve_credit_statement+pause": dml.reserve_credit_statement(
            PT, "ws", 5, shard=0, check_pause=True
        )[0],
        "reserve_credit_with_pause": _issued(
            lambda tx: dml.reserve_credit_with_pause(tx, PT, "ws", 5, shard=0)
        ),
        "reserve_key_statement": dml.reserve_key_statement(PT, "kh", 5, is_byok=False)[0],
        "reserve_key_statement+byok": dml.reserve_key_statement(PT, "kh", 5, is_byok=True)[0],
        "reserve_key": _issued(lambda tx: dml.reserve_key(tx, PT, "kh", 5, is_byok=False)),
        "reserve_strict_key": _strict(is_byok=False, windows=False),
        "reserve_strict_key+windows": _strict(is_byok=False, windows=True),
    }


def _unhinted() -> dict[str, str]:
    """name -> statement; every name but the strict BYOK ones is also in _hinted."""
    return {
        "reserve_credit": _issued(
            lambda tx: dml.reserve_credit(tx, PT, "ws", 5, shard=0, exclusive=False)
        ),
        "reserve_credit_with_pause": _issued(
            lambda tx: dml.reserve_credit_with_pause(tx, PT, "ws", 5, shard=0, exclusive=False)
        ),
        "reserve_key_statement": dml.reserve_key_statement(
            PT, "kh", 5, is_byok=False, exclusive=False
        )[0],
        "reserve_key": _issued(
            lambda tx: dml.reserve_key(tx, PT, "kh", 5, is_byok=False, exclusive=False)
        ),
        "reserve_strict_key": _strict(is_byok=True, windows=False),
        "reserve_strict_key+windows": _strict(is_byok=True, windows=True),
    }


_FLOORS = dict.fromkeys(("daily", "weekly", "monthly"), datetime(2026, 11, 10, tzinfo=UTC))
NO_DEBT_RELEASE = (
    "UPDATE tr_credit_balance "
    "SET reserved = reserved - @hold, total_usage = total_usage + @actual "
    "WHERE workspace_id=@ws AND shard=@shard AND reserved >= @hold "
    "AND NOT COALESCE(in_debt, FALSE) "
    "AND (@hold <= @actual OR NOT EXISTS ("
    "SELECT 1 FROM tr_trust_event "
    "WHERE workspace_id=@ws AND kind='payment' AND unrecovered_micro>0))"
)


def _hinted_releases() -> dict[str, str]:
    key = {
        f"release_key_statement[{windows}{'+byok' if byok else ''}]": dml.release_key_statement(
            PT, "kh", 5, 3, book_to_byok=byok, window_floors=_FLOORS, windows=windows
        )[0]
        for windows in ("current", "stale", "any")
        for byok in (False, True)
    }
    return {
        "lock_credit_release_statement": dml.lock_credit_release_statement(PT, "ws", 5, shard=0)[0],
        "release_credit": _issued(lambda tx: dml.release_credit(tx, PT, "ws", 5, 3, shard=0)),
        **key,
    }


def test_the_constant_is_the_documented_hint() -> None:
    assert dml.EXCLUSIVE_SCAN_HINT == HINT


@pytest.mark.parametrize("name", sorted(_hinted()))
def test_a_reserve_scans_exclusively_by_default(name: str) -> None:
    sql = _hinted()[name]
    assert sql.startswith(HINT + "UPDATE tr_"), sql
    assert sql.count("@{") == 1
    # One row, no second relation: the hint covers every range the statement scans.
    assert "SELECT" not in sql.upper(), sql


@pytest.mark.parametrize("name", sorted(_unhinted()))
def test_the_unhinted_variant_is_the_same_statement_without_the_hint(name: str) -> None:
    sql = _unhinted()[name]
    assert "@{" not in sql, sql
    assert sql.startswith("UPDATE tr_"), sql
    # Only the lock mode differs: the predicate and the write are identical.
    assert HINT + sql == _hinted()[name]


@pytest.mark.parametrize("name", sorted(_hinted_releases()))
def test_a_release_scans_exclusively(name: str) -> None:
    sql = _hinted_releases()[name]
    assert sql.startswith(HINT + "UPDATE tr_"), sql
    assert sql.count("@{") == 1
    # One row, no second relation: the hint covers every range the statement scans.
    assert "SELECT" not in sql.upper(), sql
    # The scan is one primary key: both key columns by equality, joined to the
    # rest of the predicate by AND, so it can never widen to a range.
    table = sql.removeprefix(HINT + "UPDATE ").split(" ", 1)[0]
    where = sql.split(" WHERE ", 1)[1]
    point = {
        "tr_credit_balance": "workspace_id=@ws AND shard=@shard AND reserved >= @hold",
        "tr_key_limit": "key_hash=@kh AND shard=@shard AND reserved >= @hold",
    }[table]
    assert where.startswith(point), sql
    assert where == point or where[len(point):].startswith((" AND ", " THEN RETURN ")), sql


def test_the_sequential_credit_release_is_its_statement_behind_the_hint() -> None:
    assert _hinted_releases()["release_credit"] == HINT + (
        "UPDATE tr_credit_balance "
        "SET reserved = reserved - @hold, total_usage = total_usage + @actual "
        "WHERE workspace_id=@ws AND shard=@shard AND reserved >= @hold "
        "THEN RETURN total_credits - total_usage - reserved, COALESCE(in_debt, FALSE)"
    )


def test_the_one_commit_release_keeps_its_text_its_subquery_and_no_hint() -> None:
    # The hint would lock a workspace's whole tr_trust_event range. The
    # statement is unchanged; the statement in front of it takes the lock.
    sql, params, types = dml.release_credit_no_debt_statement(PT, "ws", 5, 3, shard=0)
    assert sql == NO_DEBT_RELEASE
    assert "@{" not in sql and "SELECT 1 FROM tr_trust_event" in sql
    assert params == {"hold": 5, "actual": 3, "ws": "ws", "shard": 0}
    assert set(types) == set(params)


def test_the_lock_statement_scans_what_the_release_scans_and_moves_nothing() -> None:
    sql, params, types = dml.lock_credit_release_statement(PT, "ws", 5, shard=2)
    assert sql == HINT + (
        "UPDATE tr_credit_balance "
        "SET reserved = reserved, total_usage = total_usage "
        "WHERE workspace_id=@ws AND shard=@shard AND reserved >= @hold "
        "AND NOT COALESCE(in_debt, FALSE)"
    )
    # The release's own row predicates, word for word, and nothing more: it
    # matches every row the release can match. Its SET list is the release's
    # two columns, each written back to itself.
    row_predicates = NO_DEBT_RELEASE.split(" WHERE ", 1)[1].split(" AND (@hold <= @actual", 1)[0]
    assert sql.split(" WHERE ", 1)[1] == row_predicates
    assert params == {"hold": 5, "ws": "ws", "shard": 2}
    assert types == {"hold": "INT64", "ws": "STRING", "shard": "INT64"}


# Either operand order: `reserved = reserved + @est` and `reserved = @est + reserved`.
_ADDS_TO_RESERVED = re.compile(r"\breserved\s*=\s*(?:reserved\s*\+|[^,=]*\+\s*reserved\b)")


@pytest.mark.parametrize(
    ("text", "adds"),
    [
        ("SET reserved = reserved + @est", True),
        ("SET reserved=reserved+@est", True),
        ("SET reserved = @est + reserved", True),
        ("SET reserved = reserved - @est", False),
        ("SET total_usage = total_usage + @a, reserved = reserved - @est", False),
        ("SET usage = usage + reserved", False),
    ],
)
def test_the_pin_recognises_an_addition_to_reserved(text: str, adds: bool) -> None:
    assert bool(_ADDS_TO_RESERVED.search(text)) is adds


def _is_the_hint_or_nothing(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.IfExp)
        and isinstance(node.body, ast.Constant)
        and isinstance(node.orelse, ast.Constant)
        and {node.body.value, node.orelse.value} == {HINT, ""}
    )


def _reserve_sites() -> dict[tuple[str, str], str]:
    """(file, function) -> how each string in src that adds to `reserved` is hinted."""
    sites: dict[tuple[str, str], str] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "postgres" in path.name:
            continue
        tree = ast.parse(path.read_text())
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if not _ADDS_TO_RESERVED.search(node.value):
                continue
            parent = parents[node]
            if isinstance(parent, ast.Expr):
                continue  # a docstring
            if node.value.startswith(HINT):
                how = "always"
            elif (
                isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Add)
                and parent.right is node and _is_the_hint_or_nothing(parent.left)
            ):
                how = "by argument"
            else:
                how = "never"
            scope: ast.AST | None = parent
            while scope is not None and not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
                scope = parents.get(scope)
            key = (path.name, scope.name if scope is not None else "<module>")
            assert key not in sites, key
            sites[key] = how
    return sites


def test_every_statement_in_src_that_adds_to_reserved_is_accounted_for() -> None:
    """A new reserve, or one rewritten without the hint, must be decided here.

    An unhinted reserve of a hot row brings the upgrade aborts back; a hinted
    one that can match no row and carry on holds the row until the commit.
    """
    assert _reserve_sites() == {
        ("storage_gcp_counter_dml.py", "reserve_credit"): "by argument",
        ("storage_gcp_counter_dml.py", "reserve_credit_statement"): "always",
        ("storage_gcp_counter_dml.py", "reserve_credit_with_pause"): "by argument",
        ("storage_gcp_counter_dml.py", "reserve_key_statement"): "by argument",
        ("storage_gcp_strict_budget.py", "reserve_strict_key"): "by argument",
    }


# Either operand order is an addition; a release subtracts and has one order.
_TAKES_FROM_RESERVED = re.compile(r"\breserved\s*=\s*reserved\s*-")


def _release_sites() -> dict[tuple[str, str], list[bool]]:
    """(file, function) -> whether each string in src that takes from `reserved` starts hinted."""
    sites: dict[tuple[str, str], list[bool]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "postgres" in path.name:
            continue
        tree = ast.parse(path.read_text())
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                continue
            if not _TAKES_FROM_RESERVED.search(node.value):
                continue
            scope: ast.AST | None = parents[node]
            if isinstance(scope, ast.Expr):
                continue  # a docstring
            while scope is not None and not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
                scope = parents.get(scope)
            key = (path.name, scope.name if scope is not None else "<module>")
            sites.setdefault(key, []).append(node.value.startswith(HINT))
    return sites


def test_every_statement_in_src_that_takes_from_reserved_is_accounted_for() -> None:
    """A new release, or one rewritten without the hint, must be decided here.

    An unhinted release of a hot row brings the upgrade aborts back. The one
    unhinted Spanner release is the folded batch's, which has a subquery and
    runs only behind lock_credit_release_statement (pinned below). The last
    entry is the Postgres plane's, which locks its rows another way.
    """
    assert _release_sites() == {
        ("storage_gcp_counter_dml.py", "release_credit"): [True],
        ("storage_gcp_counter_dml.py", "release_credit_no_debt_statement"): [False],
        ("storage_gcp_counter_dml.py", "release_key_statement"): [True, True],
        ("storage_legacy_trust.py", "reject_postgres_reservation"): [False],
    }


def _releases_seen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str, bool]]:
    """(transaction, statement kind, hinted) for every counter-row statement a settle issues."""
    seen: list[tuple[Any, str, bool]] = []

    def record(method: str) -> None:
        original = getattr(_FakeTransaction, method)

        def call(tx: Any, sql: str, *, params: Any = None, param_types: Any = None) -> Any:
            body = sql.removeprefix(HINT)
            kind = next((name for name, prefix in {
                "lock": "UPDATE tr_credit_balance SET reserved = reserved, ",
                "credit": "UPDATE tr_credit_balance SET reserved = reserved - @hold",
                "key": "UPDATE tr_key_limit SET reserved = reserved - @hold",
            }.items() if body.startswith(prefix)), None)
            if kind is not None and not getattr(tx, "_in_returning", False):
                seen.append((tx, kind, sql.startswith(HINT)))
            return original(tx, sql, params=params, param_types=param_types)

        monkeypatch.setattr(_FakeTransaction, method, call)

    record("execute_update")
    record("execute_sql")
    return seen


def _per_transaction(seen: list[tuple[Any, str, bool]]) -> list[list[tuple[str, bool]]]:
    transactions = list(dict.fromkeys(tx for tx, _, _ in seen))
    return [[(kind, hinted) for tx, kind, hinted in seen if tx is each] for each in transactions]


def test_the_folded_batch_locks_the_credit_row_immediately_before_its_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db, options = _settle_fixture()
    seen = _releases_seen(monkeypatch)
    assert _settle(db, options)["outcome"] == "settled"
    # One transaction: the lock statement, the unhinted release it locks for,
    # then the key release in both window forms, each hinted.
    assert _per_transaction(seen) == [
        [("lock", True), ("credit", False), ("key", True), ("key", True)],
    ]
    assert db.credit_release_locks == 1
    assert db.releases_without_exclusive_lock == 0
    assert db.typed["tr_credit_balance"][("workspace", 0)]["total_usage"] == 70
    assert db.typed["tr_credit_balance"][("workspace", 0)]["reserved"] == 0
    assert db.typed["tr_key_limit"][("key", 0)]["usage"] == 70


def test_the_fake_counts_a_folded_release_that_was_not_locked_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The check must be able to fail: without the lock statement the batch
    # still settles the same money, and the fake reports the shared first touch.
    db, options = _settle_fixture()
    monkeypatch.setattr(
        dml, "lock_credit_release_statement",
        lambda pt, ws, hold, *, shard: (
            "UPDATE tr_credit_balance SET total_usage = total_usage "
            "WHERE workspace_id=@ws AND shard=@shard",
            {"ws": ws, "shard": shard}, {"ws": pt.STRING, "shard": pt.INT64},
        ),
    )
    original = _FakeTransaction.execute_update

    def execute_update(tx: Any, sql: str, **kwargs: Any) -> int:
        if sql.startswith("UPDATE tr_credit_balance SET total_usage = total_usage WHERE"):
            return 1
        return original(tx, sql, **kwargs)

    monkeypatch.setattr(_FakeTransaction, "execute_update", execute_update)
    assert _settle(db, options)["outcome"] == "settled"
    assert db.credit_release_locks == 0
    assert db.releases_without_exclusive_lock == 1
    assert db.typed["tr_credit_balance"][("workspace", 0)]["total_usage"] == 70


@pytest.mark.parametrize("shape", ["sequential", "refund", "marked_in_debt", "reaper"])
def test_every_other_release_path_takes_both_rows_exclusively(
    monkeypatch: pytest.MonkeyPatch, shape: str,
) -> None:
    db, options = _settle_fixture()
    if shape == "refund":
        refunded = options["authorization"]
        refunded.record_finalization(
            success=False, actual_microdollars=0, selected_usage_type="Credits", generation=None,
        )
        options.update(
            success=False, actual_micro=0, generation=None, generation_writes=[],
            authorization=refunded, auth_body_settled=json_body(refunded),
            settle_outbox_done=(options["authorization_id"], "settle"),
        )
    elif shape == "marked_in_debt":
        # The batch's lock statement and release both match nothing on a
        # marked row; the batch rolls back and the sequential release runs.
        db.typed["tr_credit_balance"][("workspace", 0)]["in_debt"] = True
    seen = _releases_seen(monkeypatch)
    if shape == "reaper":
        reaped = authorize._finalize_reaped_reservation_atomic(
            db, param_types, reservation_id=options["reservation_id"],
            reap_now=NOW + timedelta(minutes=6), guard_outbox=False,
            snapshot_booking_enabled=False, operational_analytics_outbox=None,
        )
        assert reaped.outcome == "settled"
    elif shape == "sequential":
        # An outbox without the statement-building API keeps the sequential path.
        inner = SpannerOperationalAnalyticsOutbox(db, param_types)
        sequential = SimpleNamespace(enqueue_activity_tx=inner.enqueue_activity_tx)
        result = authorize.typed_finalize_atomic(
            db, param_types, operational_analytics_outbox=sequential, **options,
        )
        assert result["outcome"] == "settled"
    else:
        assert _settle(db, options)["outcome"] == "settled"
    transactions = _per_transaction(seen)
    final = transactions[-1]
    # The committed transaction releases credit then key, each hinted. The
    # key's current form may miss and be followed by the rolling form.
    assert final[0] == ("credit", True), transactions
    assert final[1:] and all(entry == ("key", True) for entry in final[1:]), transactions
    if shape == "marked_in_debt":
        # The rolled-back batch held the row exclusively from its lock statement.
        assert transactions[0][0] == ("lock", True), transactions
        assert len(transactions) == 2
    else:
        assert len(transactions) == 1, transactions
    assert db.releases_without_exclusive_lock == 0
    assert db.typed["tr_credit_balance"][("workspace", 0)]["reserved"] == 0
    assert db.typed["tr_key_limit"][("key", 0)]["reserved"] == 0


def _authorize_reserves(monkeypatch: pytest.MonkeyPatch, db: Any, **overrides: Any) -> list[tuple]:
    """(hinted, table, shard) for every reserve authorize issued, in order."""
    reserves: list[tuple] = []

    def record(method: str) -> None:
        original = getattr(_FakeTransaction, method)

        def call(tx: Any, sql: str, *, params: Any = None, param_types: Any = None) -> Any:
            body = sql.removeprefix(HINT)
            if body.startswith((CREDIT, KEY)) and not getattr(tx, "_in_returning", False):
                table = "credit" if body.startswith(CREDIT) else "key"
                reserves.append((sql.startswith(HINT), table, params.get("shard", 0)))
            return original(tx, sql, params=params, param_types=param_types)

        monkeypatch.setattr(_FakeTransaction, method, call)

    record("execute_update")
    record("execute_sql")
    db.now = NOW
    authorize.authorize_atomic(db, param_types, **{**_options(), **overrides})
    return reserves


def _two_credit_shards(first_funded: bool) -> Any:
    db = _database()
    row = db.typed["tr_credit_balance"][("workspace", 0)]
    db.typed["tr_credit_balance"][("workspace", 1)] = {**row, "shard": 1}
    if not first_funded:
        row["total_credits"] = 0
    return db


_ARMED = SimpleNamespace(spend_lease_trust_eligibility_enabled=True)
_BYOK = {"has_credit_candidate": False, "reservation_usage_type": "BYOK"}


@pytest.mark.parametrize("trust", [None, _ARMED], ids=["unarmed", "armed"])
def test_the_batch_reserves_are_hinted(monkeypatch: pytest.MonkeyPatch, trust: Any) -> None:
    # Speculative shape: both reserves ride one batch, and a miss rolls back at once.
    assert _authorize_reserves(monkeypatch, _database(), trust_settings=trust) == [
        (True, "credit", 0), (True, "key", 0),
    ]


@pytest.mark.parametrize("include_byok", [False, True])
def test_the_batch_key_reserve_is_hinted_for_byok_too(
    monkeypatch: pytest.MonkeyPatch, include_byok: bool,
) -> None:
    # A batch miss rolls back at once, so the hinted statement never holds the
    # key row through a commit. On a key that excludes BYOK the batch misses
    # and the retry is the unhinted sequential reserve.
    db = _database()
    db.typed["tr_key_limit"][("key", 0)]["include_byok"] = include_byok
    reserves = _authorize_reserves(monkeypatch, db, **_BYOK)
    assert reserves == [(True, "key", 0)] + ([] if include_byok else [(False, "key", 0)])
    assert db.typed["tr_key_limit"][("key", 0)]["reserved"] == (100 if include_byok else 0)


@pytest.mark.parametrize("trust", [None, _ARMED], ids=["unarmed", "armed"])
def test_a_sequential_reserve_of_one_shard_is_hinted(
    monkeypatch: pytest.MonkeyPatch, trust: Any,
) -> None:
    reserves = _authorize_reserves(
        monkeypatch, _database(), speculate_key_limit=False, trust_settings=trust,
    )
    assert reserves == [(True, "credit", 0), (True, "key", 0)]


@pytest.mark.parametrize("trust", [None, _ARMED], ids=["unarmed", "armed"])
def test_a_sequential_probe_of_several_credit_shards_is_not_hinted(
    monkeypatch: pytest.MonkeyPatch, trust: Any,
) -> None:
    # Two probers that miss different shards and then try each other's would
    # each hold one exclusively and wait for the other.
    reserves = _authorize_reserves(
        monkeypatch, _two_credit_shards(first_funded=False), speculate_key_limit=False,
        credit_shard_candidates=(0, 1), trust_settings=trust,
    )
    assert reserves == [(False, "credit", 0), (False, "credit", 1), (True, "key", 0)]


def test_a_missed_batch_falls_back_to_unhinted_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    reserves = _authorize_reserves(
        monkeypatch, _two_credit_shards(first_funded=False), credit_shard_candidates=(0, 1),
    )
    # The hinted batch missed credit shard zero and rolled back, taking its key
    # hold with it; the retry probes both credit shards unhinted.
    assert reserves == [
        (True, "credit", 0), (True, "key", 0),
        (False, "credit", 0), (False, "credit", 1), (True, "key", 0),
    ]


def test_a_sequential_probe_of_several_key_shards_is_not_hinted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _database()
    row = db.typed["tr_key_limit"][("key", 0)]
    db.typed["tr_key_limit"][("key", 1)] = {**row, "shard": 1}
    row["limit_micro"] = 0
    reserves = _authorize_reserves(
        monkeypatch, db, speculate_key_limit=False, key_shard_candidates=(0, 1),
    )
    assert reserves == [(True, "credit", 0), (False, "key", 0), (False, "key", 1)]


@pytest.mark.parametrize("include_byok", [False, True])
def test_a_sequential_byok_key_reserve_is_not_hinted(
    monkeypatch: pytest.MonkeyPatch, include_byok: bool,
) -> None:
    # A key that excludes BYOK matches no row on every BYOK request, and the
    # transaction commits. Hinted, those requests would queue on the key row.
    db = _database()
    db.typed["tr_key_limit"][("key", 0)]["include_byok"] = include_byok
    reserves = _authorize_reserves(monkeypatch, db, speculate_key_limit=False, **_BYOK)
    assert reserves == [(False, "key", 0)]
    assert db.typed["tr_key_limit"][("key", 0)]["reserved"] == (100 if include_byok else 0)


@pytest.mark.parametrize(("byok", "hinted"), [(False, True), (True, False)])
def test_the_strict_key_reserve_is_hinted_unless_byok(
    monkeypatch: pytest.MonkeyPatch, byok: bool, hinted: bool,
) -> None:
    reserves = _authorize_reserves(
        monkeypatch, _database(), strict_budget=True, **(_BYOK if byok else {}),
    )
    assert reserves[-1] == (hinted, "key", 0)
    # Strict budget takes the sequential credit path: one shard, so hinted.
    assert reserves[:-1] == ([] if byok else [(True, "credit", 0)])


def _subquery_statement() -> tuple[str, dict, dict]:
    sql, params, types = dml.release_credit_no_debt_statement(PT, "ws", 5, 3, shard=0)
    return HINT + sql, params, types


def test_fake_accepts_the_hint_and_refuses_it_with_a_subquery() -> None:
    plain = "UPDATE tr_credit_balance SET reserved = reserved + @est WHERE workspace_id=@ws"
    assert fake_spanner._without_scan_hint(HINT + plain) == plain
    assert fake_spanner._without_scan_hint(plain) == plain
    with pytest.raises(AssertionError, match="subquery"):
        fake_spanner._without_scan_hint(_subquery_statement()[0])


@pytest.mark.parametrize("entry", ["execute_update", "execute_sql", "batch_update"])
def test_every_fake_entry_point_refuses_the_hint_with_a_subquery(entry: str) -> None:
    # The refusal is only worth something where statements actually arrive.
    sql, params, types = _subquery_statement()
    tx = _FakeTransaction(_database())
    with pytest.raises(AssertionError, match="subquery"):
        if entry == "batch_update":
            tx.batch_update([(sql, params, types)])
        else:
            getattr(tx, entry)(sql, params=params, param_types=types)
