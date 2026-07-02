"""The fake Spanner must model real-Spanner semantics that have leaked prod
bugs — otherwise green tests give false safety on the money path."""

from __future__ import annotations

import pytest

from tests.fakes.spanner import FakeSpannerDatabase


def _db() -> FakeSpannerDatabase:
    return FakeSpannerDatabase()


def test_single_use_snapshot_raises_on_second_read() -> None:
    """Real Spanner: a single-use snapshot permits exactly ONE read; the second
    raises. Prod bug fa9f5d4 was a single-use snapshot that grew a second read.
    The fake must fault so CI catches it, not prod."""
    db = _db()
    with db.snapshot() as snap:
        snap.execute_sql("SELECT total_credits FROM tr_credit_balance WHERE workspace_id=@pk",
                         params={"pk": "ws_x"})
        with pytest.raises(ValueError, match="single-use snapshot"):
            snap.execute_sql("SELECT total_credits FROM tr_credit_balance WHERE workspace_id=@pk",
                             params={"pk": "ws_x"})


def test_multi_use_snapshot_allows_multiple_reads() -> None:
    db = _db()
    with db.snapshot(multi_use=True) as snap:
        for _ in range(3):
            snap.execute_sql("SELECT total_credits FROM tr_credit_balance WHERE workspace_id=@pk",
                             params={"pk": "ws_x"})  # no raise
