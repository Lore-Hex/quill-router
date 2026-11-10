"""The authorize-side reserves lock their row exclusively at the scan.

docs/incidents/2026-11-10-reserve-lock-upgrade-aborts.md: a conditional UPDATE
scans under a shared lock and upgrades it, so simultaneous reserves of one row
abort each other. The statement hint removes the upgrade. These tests pin which
statements carry it, and that no hinted statement has a subquery (the hint
would lock the subquery's range too).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.fakes import spanner as fake_spanner
from trusted_router import storage_gcp_counter_dml as dml
from trusted_router.storage_gcp_strict_budget import reserve_strict_key

HINT = "@{LOCK_SCANNED_RANGES=EXCLUSIVE} "
PT = SimpleNamespace(INT64="INT64", STRING="STRING", BOOL="BOOL", TIMESTAMP="TIMESTAMP")
SRC = Path(__file__).resolve().parents[1] / "src" / "trusted_router"


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


def _reserves() -> dict[str, str]:
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
        "reserve_key": _issued(lambda tx: dml.reserve_key(tx, PT, "kh", 5, is_byok=False)),
        "reserve_strict_key": _issued(
            lambda tx: reserve_strict_key(tx, PT, "kh", 5, is_byok=False, enforce_windows=False)
        ),
        "reserve_strict_key+windows": _issued(
            lambda tx: reserve_strict_key(tx, PT, "kh", 5, is_byok=True, enforce_windows=True)
        ),
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


@pytest.mark.parametrize("name", sorted(_reserves()))
def test_every_reserve_scans_exclusively(name: str) -> None:
    sql = _reserves()[name]
    assert sql.startswith(HINT + "UPDATE tr_"), sql
    assert sql.count("@{") == 1
    # One row, no second relation: the hint covers every range the statement scans.
    assert "SELECT" not in sql.upper(), sql


@pytest.mark.parametrize("name", sorted(_releases()))
def test_no_release_carries_the_hint(name: str) -> None:
    # The one-commit credit release has a tr_trust_event subquery; the hint
    # would lock a workspace's whole event range. The releases stay as they are
    # until that statement is restructured.
    assert "@{" not in _releases()[name]


def test_the_one_commit_release_still_has_the_subquery_that_forbids_the_hint() -> None:
    sql = dml.release_credit_no_debt_statement(PT, "ws", 5, 3, shard=0)[0]
    assert "SELECT 1 FROM tr_trust_event" in sql


def test_no_reserve_statement_in_src_is_left_unhinted() -> None:
    """A new reserve written without the hint brings the upgrade aborts back."""
    reserve = re.compile(r'"UPDATE tr_\w+ SET reserved = reserved \+ @est ')
    found = 0
    for path in sorted(SRC.rglob("*.py")):
        if "postgres" in path.name:
            continue
        lines = path.read_text().splitlines()
        for number, line in enumerate(lines):
            if reserve.search(line):
                found += 1
                assert "EXCLUSIVE_SCAN_HINT" in lines[number - 1], f"{path.name}:{number + 1}"
    assert found == 5, found


def test_fake_accepts_the_hint_and_refuses_it_with_a_subquery() -> None:
    plain = "UPDATE tr_credit_balance SET reserved = reserved + @est WHERE workspace_id=@ws"
    assert fake_spanner._without_scan_hint(HINT + plain) == plain
    assert fake_spanner._without_scan_hint(plain) == plain
    with pytest.raises(AssertionError, match="subquery"):
        fake_spanner._without_scan_hint(
            HINT + dml.release_credit_no_debt_statement(PT, "ws", 5, 3, shard=0)[0]
        )
