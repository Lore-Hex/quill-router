"""The pilot key-usage backfill plan (scripts/backfill_leased_key_usage.py):
each settled leased charge once, to the lifetime counter and to the windows
its settle time falls in; nothing the finalize books itself or the records
do not confirm; and the UPDATE as release_key's window SQL would book it."""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "backfill_leased_key_usage", Path(__file__).resolve().parents[1] / "scripts" / "backfill_leased_key_usage.py",
)
assert _SPEC is not None and _SPEC.loader is not None
backfill = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = backfill
_SPEC.loader.exec_module(backfill)

A1, A2, A3, A4, A5 = ("gwa-" + digit * 32 for digit in "12345")
KEY = "key_v2cQvb7NjlDKPiSAJ9qnD-RE"
NOW = dt.datetime(2026, 10, 18, 18, 0, tzinfo=dt.UTC)  # a Sunday; the week floor is Monday 10-12
UNTIL = dt.datetime(2026, 10, 18, 17, 0, tzinfo=dt.UTC)


def _won(aid: str, at: str, charge: int) -> str:
    return f"{at}\tINFO trusted_router.fastpath fastpath pilot terminal won authorization_id={aid} kind=settle won=settle charge={charge} outcome="


def _row(aid: str, cost: int, *, outcome: str = "settled", settled: bool = True) -> dict:
    return {
        "authorization_id": aid, "key_hash": KEY, "workspace_id": "ws-1", "settled": settled,
        "finalization_outcome": outcome, "finalized_cost_microdollars": cost,
    }


def test_the_log_names_settled_leased_authorizations_their_time_and_charge_and_the_deferred() -> None:
    """Lines in any order (the log reads newest first). A1 settled once,
    with its authorize line before it. A4 settled and was named after by
    any line, a failed terminal read here, so the drain may have finished
    it. A5's latest won line counts, and the earlier one does not. A2 and A3
    are no settles."""
    lines = [
        f"2026-10-17T05:10:00Z\tERROR ... fastpath pilot terminal read failed, left to the drain authorization_id={A4}",
        _won(A4, "2026-10-17T05:09:00Z", 50),
        _won(A5, "2026-10-17T05:08:00Z", 70),
        _won(A5, "2026-10-17T05:07:00Z", 60),
        f"2026-10-17T05:00:01Z\tINFO ... fastpath pilot terminal won authorization_id={A2} kind=refund won=refund charge=0 outcome=",
        f"2026-10-17T05:00:02Z\tINFO ... fastpath pilot terminal settled authorization_id={A3} kind=settle won= charge=0 outcome=settled",
        _won(A1, "2026-10-17T05:00:00.123456Z", 306),
        f"2026-10-17T04:59:59Z\tINFO ... fastpath pilot authorize admitted workspace_id=w authorization_id={A1} ms=9.9",
    ]
    settled, deferred = backfill.parse_log(lines)
    assert settled == {
        A1: (dt.datetime(2026, 10, 17, 5, 0, 0, 123456, tzinfo=dt.UTC), 306),
        A4: (dt.datetime(2026, 10, 17, 5, 9, tzinfo=dt.UTC), 50),
        A5: (dt.datetime(2026, 10, 17, 5, 8, tzinfo=dt.UTC), 70),
    }
    assert deferred == {A4}


def test_the_plan_books_each_charge_once_into_the_windows_its_settle_time_falls_in() -> None:
    settled = {
        A1: (dt.datetime(2026, 10, 17, 23, 59, tzinfo=dt.UTC), 100),  # yesterday: lifetime, week, month; not today
        A2: (dt.datetime(2026, 10, 18, 6, 0, tzinfo=dt.UTC), 20),  # today: every window
        A3: (dt.datetime(2026, 10, 18, 17, 30, tzinfo=dt.UTC), 7),  # on or after --until: the finalize's own
        A4: (dt.datetime(2026, 10, 18, 7, 0, tzinfo=dt.UTC), 9),  # the record disagrees with the log
        A5: (dt.datetime(2026, 10, 18, 8, 0, tzinfo=dt.UTC), 4),  # the drain was left to finish it
    }
    rows = [_row(A1, 100), _row(A2, 20), _row(A3, 7), _row(A4, 5), _row(A5, 4)]
    entries, skipped = backfill.plan(rows, settled, {A5}, until=UNTIL, now=NOW)
    assert entries == [{"workspace_id": "ws-1", "key_hash": KEY, "usage": 120, "day": 20, "week": 120, "month": 120, "count": 2}]
    assert sorted(line.split(":")[0] for line in skipped) == sorted([A3, A4, A5])


def test_the_plan_skips_what_is_not_a_settled_record_and_names_missing_rows() -> None:
    settled = {A1: (NOW, 100), A2: (NOW, 50)}
    rows = [_row(A1, 100, outcome="refunded")]
    entries, skipped = backfill.plan(rows, settled, set(), until=UNTIL, now=NOW)
    assert entries == []
    assert sorted(line.split(":")[0] for line in skipped) == [A1, A2]


def test_the_update_refuses_a_key_hash_outside_its_charset() -> None:
    with pytest.raises(ValueError):
        backfill.update_sql({"key_hash": "k' OR 1=1", "usage": 1, "day": 1, "week": 1, "month": 1}, now=NOW)


def test_the_update_rolls_a_stale_window_forward_and_adds_to_a_current_one() -> None:
    entry = {"workspace_id": "ws-1", "key_hash": KEY, "usage": 120, "day": 20, "week": 120, "month": 120, "count": 2}
    sql = backfill.update_sql(entry, now=NOW)
    assert sql.startswith("UPDATE tr_key_limit SET usage = usage + 120, ")
    assert "day_usage = IF(day_start IS NULL OR day_start < TIMESTAMP '2026-10-18T00:00:00Z', 20, COALESCE(day_usage, 0) + 20)" in sql
    assert "week_start = IF(week_start IS NULL OR week_start < TIMESTAMP '2026-10-12T00:00:00Z', TIMESTAMP '2026-10-12T00:00:00Z', week_start)" in sql
    assert "month_usage = IF(month_start IS NULL OR month_start < TIMESTAMP '2026-10-01T00:00:00Z', 120, COALESCE(month_usage, 0) + 120)" in sql
    assert sql.endswith(f" WHERE key_hash = '{KEY}' AND shard = 0")
