"""The pilot key-usage backfill (scripts/backfill_leased_key_usage.py): its
plan books each settled leased charge once, to the lifetime counter and to
the windows whose current floor the charge is within, and leaves out what
the finalize books itself or the records do not confirm."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "backfill_leased_key_usage", Path(__file__).resolve().parents[1] / "scripts" / "backfill_leased_key_usage.py",
)
assert _SPEC is not None and _SPEC.loader is not None
backfill = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = backfill
_SPEC.loader.exec_module(backfill)

A1 = "gwa-" + "1" * 32
A2 = "gwa-" + "2" * 32
A3 = "gwa-" + "3" * 32
A4 = "gwa-" + "4" * 32
KEY = "a" * 64
NOW = dt.datetime(2026, 10, 18, 18, 0, tzinfo=dt.UTC)  # a Sunday; the week floor is Monday 10-12
UNTIL = dt.datetime(2026, 10, 18, 17, 0, tzinfo=dt.UTC)


def _row(aid: str, created: str, cost: int, *, outcome: str = "settled", settled: bool = True) -> dict:
    return {
        "authorization_id": aid, "key_hash": KEY, "workspace_id": "ws-1", "settled": settled,
        "finalization_outcome": outcome, "finalized_cost_microdollars": cost, "created_at": created,
    }


def test_the_log_names_settled_leased_authorizations_and_their_charges() -> None:
    lines = [
        f"2026-10-17T05:00:00Z\tINFO trusted_router.fastpath fastpath pilot terminal won authorization_id={A1} kind=settle won=settle charge=306 outcome=",
        f"2026-10-17T05:00:01Z\tINFO ... fastpath pilot terminal won authorization_id={A2} kind=refund won=refund charge=0 outcome=",
        f"2026-10-17T05:00:02Z\tINFO ... fastpath pilot terminal settled authorization_id={A3} kind=settle won= charge=0 outcome=settled",
    ]
    assert backfill.parse_log(lines) == {A1: 306}


def test_the_plan_books_each_charge_once_into_the_windows_it_falls_in() -> None:
    charges = {A1: 100, A2: 20, A3: 7, A4: 5}
    rows = [
        _row(A1, "2026-10-17T05:00:00Z", 100),  # yesterday: lifetime, week, month; not today
        _row(A2, "2026-10-18T06:00:00Z", 20),  # today: every window
        _row(A3, "2026-10-18T17:30:00Z", 7),  # on or after --until: the finalize's own
        _row(A4, "2026-10-18T07:00:00Z", 9),  # the record disagrees with the log
    ]
    entries, skipped = backfill.plan(rows, charges, until=UNTIL, now=NOW)
    assert entries == [{"workspace_id": "ws-1", "key_hash": KEY, "usage": 120, "day": 20, "week": 120, "month": 120, "count": 2}]
    assert [line.split(":")[0] for line in skipped] == [A3, A4]


def test_the_plan_skips_what_is_not_a_settled_record_and_names_missing_rows() -> None:
    charges = {A1: 100, A2: 50}
    rows = [_row(A1, "2026-10-18T06:00:00Z", 100, outcome="refunded", settled=True)]
    entries, skipped = backfill.plan(rows, charges, until=UNTIL, now=NOW)
    assert entries == []
    assert sorted(line.split(":")[0] for line in skipped) == [A1, A2]


def test_the_update_rolls_a_stale_window_forward_and_adds_to_a_current_one() -> None:
    entry = {"workspace_id": "ws-1", "key_hash": KEY, "usage": 120, "day": 20, "week": 120, "month": 120, "count": 2}
    sql = backfill.update_sql(entry, now=NOW)
    assert sql.startswith("UPDATE tr_key_limit SET usage = usage + 120, ")
    assert "day_usage = IF(day_start IS NULL OR day_start < TIMESTAMP '2026-10-18T00:00:00Z', 20, COALESCE(day_usage, 0) + 20)" in sql
    assert "week_start = IF(week_start IS NULL OR week_start < TIMESTAMP '2026-10-12T00:00:00Z', TIMESTAMP '2026-10-12T00:00:00Z', week_start)" in sql
    assert "month_usage = IF(month_start IS NULL OR month_start < TIMESTAMP '2026-10-01T00:00:00Z', 120, COALESCE(month_usage, 0) + 120)" in sql
    assert sql.endswith(f" WHERE key_hash = '{KEY}' AND shard = 0")
