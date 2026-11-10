"""Which authorize-side reserves lock their row exclusively at the scan.

docs/incidents/2026-11-10-reserve-lock-upgrade-aborts.md: a conditional UPDATE
scans under a shared lock and upgrades it, so simultaneous reserves of one row
abort each other. The statement hint removes the upgrade, but a hinted
statement that matches no row holds the exclusive lock until its transaction
ends. So a reserve carries the hint only where it is expected to write and is
the only counter row of its table the transaction touches. These tests pin
which statements carry it, in which transaction shapes, and that no hinted
statement has a subquery (the hint would lock the subquery's range too).
"""

from __future__ import annotations

import ast
import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from google.cloud.spanner_v1 import param_types

from tests.fakes import spanner as fake_spanner
from tests.fakes.spanner import _FakeTransaction
from tests.test_authorize_speculative_batch import _options
from tests.test_spanner_batch_dml import NOW, _database
from trusted_router import storage_gcp_authorize as authorize
from trusted_router import storage_gcp_counter_dml as dml
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


def _releases() -> dict[str, str]:
    floors = dict.fromkeys(("daily", "weekly", "monthly"), datetime(2026, 11, 10, tzinfo=UTC))
    key = {
        f"release_key_statement[{windows}]": dml.release_key_statement(
            PT, "kh", 5, 3, book_to_byok=False, window_floors=floors, windows=windows
        )[0]
        for windows in ("current", "stale", "any")
    }
    return {
        "release_credit_no_debt_statement": dml.release_credit_no_debt_statement(
            PT, "ws", 5, 3, shard=0
        )[0],
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


@pytest.mark.parametrize("name", sorted(_releases()))
def test_no_release_carries_the_hint(name: str) -> None:
    # The one-commit credit release has a tr_trust_event subquery; the hint
    # would lock a workspace's whole event range. The releases stay as they are
    # until that statement is restructured.
    assert "@{" not in _releases()[name]


def test_the_one_commit_release_still_has_the_subquery_that_forbids_the_hint() -> None:
    sql = dml.release_credit_no_debt_statement(PT, "ws", 5, 3, shard=0)[0]
    assert "SELECT 1 FROM tr_trust_event" in sql


_ADDS_TO_RESERVED = re.compile(r"\breserved\s*=\s*reserved\s*\+")


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
