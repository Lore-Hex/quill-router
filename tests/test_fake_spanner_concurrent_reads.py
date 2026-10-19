"""A fake Spanner read must never interleave with a concurrent commit.

Commits mutate the fake's shared dicts under its commit lock. A read that scans
them unlocked raised "dictionary changed size during iteration" when another
thread committed mid-scan (CI run 39199440546, social reward claim test), and
could see half of a commit, which a real Spanner reader never does.

These tests start a commit from another thread in the middle of a scan, so the
interleaving is forced rather than left to scheduler timing.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from typing import Any

from tests.fakes.spanner import FakeSpannerDatabase

_TABLE = "tr_trust_event"
_COLUMNS = ("workspace_id", "event_id")
_READ_SQL = "SELECT workspace_id, event_id FROM tr_trust_event WHERE workspace_id=@pk"


class _CommitDuringScan(dict[tuple, dict]):
    """Starts a batch commit into this table after the scan's first row.

    The commit runs on its own thread; the scan waits briefly for it. When
    reads hold the commit lock the commit cannot land until the scan ends.
    """

    def __init__(self, db: FakeSpannerDatabase, rows: dict[tuple, dict]) -> None:
        super().__init__(rows)
        self.db = db
        self.committed_during_scan: bool | None = None
        self.writer: threading.Thread | None = None

    def _commit_new_row(self) -> None:
        with self.db.batch() as batch:
            batch.insert_or_update(table=_TABLE, columns=_COLUMNS, values=[("ws", "new")])

    def _start_commit_and_wait(self) -> None:
        if self.writer is not None:
            return
        self.writer = threading.Thread(target=self._commit_new_row)
        self.writer.start()
        self.writer.join(timeout=0.5)
        self.committed_during_scan = not self.writer.is_alive()

    def __iter__(self) -> Iterator[tuple]:
        for index, key in enumerate(super().__iter__()):
            if index == 0:
                self._start_commit_and_wait()
            yield key

    def items(self) -> Any:
        return _ItemsDuringScan(self)


class _ItemsDuringScan:
    def __init__(self, table: _CommitDuringScan) -> None:
        self.table = table

    def __iter__(self) -> Iterator[tuple[tuple, dict]]:
        for index, item in enumerate(dict.items(self.table)):
            if index == 0:
                self.table._start_commit_and_wait()
            yield item


def _seeded(db: FakeSpannerDatabase) -> _CommitDuringScan:
    rows = {("ws", f"e{i}"): {"workspace_id": "ws", "event_id": f"e{i}"} for i in range(3)}
    table = _CommitDuringScan(db, rows)
    db.typed[_TABLE] = table
    return table


def _finish(table: _CommitDuringScan) -> None:
    assert table.writer is not None, "the scan never reached the commit hook"
    table.writer.join(timeout=5)
    assert not table.writer.is_alive()
    assert ("ws", "new") in dict.keys(table), "the deferred commit never landed"


def test_snapshot_read_does_not_interleave_with_a_commit() -> None:
    db = FakeSpannerDatabase()
    table = _seeded(db)

    with db.snapshot() as snapshot:
        rows = snapshot.execute_sql(_READ_SQL, params={"pk": "ws"})

    assert table.committed_during_scan is False
    assert sorted(row[1] for row in rows) == ["e0", "e1", "e2"]
    _finish(table)


def test_transaction_read_does_not_interleave_with_a_commit() -> None:
    db = FakeSpannerDatabase()
    table = _seeded(db)
    seen: list[list[Any]] = []

    def read(txn: Any) -> None:
        if not seen:
            seen.append(txn.execute_sql(_READ_SQL, params={"pk": "ws"}))

    db.run_in_transaction(read)

    assert table.committed_during_scan is False
    assert sorted(row[1] for row in seen[0]) == ["e0", "e1", "e2"]
    _finish(table)
