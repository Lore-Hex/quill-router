"""Backfill receipt-key version projection columns in bounded, resumable pages.

The deploy runs this on every release, before any serving revision changes.
The backfill finished long ago, so a normal run reads one empty page and exits
in seconds. It must never hold a deploy: on 2026-11-24 one run issued its first
page read and then never returned, and the step sat for hours inside the
control-plane deploy lease. So every page read carries an RPC deadline (in the
store), the loop stops at a wall-clock deadline between pages, and a SIGALRM
backstop ends a call that hangs anyway. On a deadline the command exits
non-zero and logs the cursor to resume from (``--after``); every page commits
on its own, so a rerun skips what is already done.
"""

from __future__ import annotations

import argparse
import logging
import signal
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from types import FrameType
from typing import Protocol, cast

from trusted_router.config import get_settings
from trusted_router.storage import create_store

log = logging.getLogger(__name__)

DEFAULT_DEADLINE_SECONDS = 600
MAX_DEADLINE_SECONDS = 3_600
DEADLINE_EXIT_CODE = 3


class BackfillDeadlineExceeded(TimeoutError):
    """The backfill ran past its wall-clock deadline; resume after ``cursor``."""

    def __init__(self, *, updated: int, cursor: str | None) -> None:
        super().__init__(
            f"receipt-key backfill deadline exceeded updated={updated} after={cursor or ''}"
        )
        self.updated = updated
        self.cursor = cursor


class _ReceiptKeyBackfillStore(Protocol):
    def backfill_receipt_key_versions_page(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> list[str]: ...


def run(
    store: _ReceiptKeyBackfillStore,
    *,
    batch_size: int = 100,
    after: str | None = None,
    deadline_seconds: float | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> int:
    if not 1 <= batch_size <= 1_000:
        raise ValueError("batch size must be between 1 and 1000")
    if deadline_seconds is not None and not 0 < deadline_seconds <= MAX_DEADLINE_SECONDS:
        raise ValueError(f"deadline must be between 0 and {MAX_DEADLINE_SECONDS} seconds")
    deadline = None if deadline_seconds is None else clock() + deadline_seconds
    updated = 0
    cursor = after
    while True:
        if deadline is not None and clock() >= deadline:
            raise BackfillDeadlineExceeded(updated=updated, cursor=cursor)
        entity_ids = store.backfill_receipt_key_versions_page(
            after=cursor,
            limit=batch_size,
        )
        if not entity_ids:
            log.info(
                "receipt_key.version_backfill_complete updated=%d after=%s",
                updated,
                cursor or "",
            )
            return updated
        if cursor is not None and entity_ids[-1] <= cursor:
            # The page query is `id > @after ORDER BY id`; a page that does not
            # move the cursor forward would loop forever.
            raise RuntimeError(
                f"receipt-key backfill cursor did not advance after={cursor}"
            )
        updated += len(entity_ids)
        cursor = entity_ids[-1]
        log.info(
            "receipt_key.version_backfill_checkpoint updated=%d after=%s",
            updated,
            cursor,
        )


@contextmanager
def _hard_deadline(seconds: int) -> Iterator[None]:
    """Interrupt a call that hangs past the deadline (main thread, POSIX)."""

    def expired(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        raise BackfillDeadlineExceeded(updated=-1, cursor=None)

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--after", help="resume after this legacy entity ID")
    parser.add_argument(
        "--deadline-seconds",
        type=int,
        default=DEFAULT_DEADLINE_SECONDS,
        help=f"stop (exit {DEADLINE_EXIT_CODE}) after this many seconds; "
        "rerun with --after to resume",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.deadline_seconds <= MAX_DEADLINE_SECONDS:
        parser.error(f"--deadline-seconds must be between 1 and {MAX_DEADLINE_SECONDS}")
    logging.basicConfig(level=logging.INFO)
    store = cast(_ReceiptKeyBackfillStore, create_store(get_settings()))
    try:
        # The backstop fires a little after the soft deadline so a normal
        # between-pages stop wins and reports its exact resume cursor.
        with _hard_deadline(args.deadline_seconds + 30):
            run(
                store,
                batch_size=args.batch_size,
                after=args.after,
                deadline_seconds=args.deadline_seconds,
            )
    except BackfillDeadlineExceeded as exc:
        log.error(
            "receipt_key.version_backfill_deadline_exceeded deadline_seconds=%d "
            "updated=%s resume_after=%s (completed pages are committed; rerun with "
            "--after to resume, or without it to rescan)",
            args.deadline_seconds,
            "unknown" if exc.updated < 0 else exc.updated,
            exc.cursor or "unknown",
        )
        return DEADLINE_EXIT_CODE
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
