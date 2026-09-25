"""Lossy, bounded post-response mirrors, isolated from billing's AnyIO pool."""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from starlette.background import BackgroundTasks

log = logging.getLogger(__name__)
WORKERS = 4
MAX_IN_FLIGHT = 64
DROP_LOG_INTERVAL_SECONDS = 60.0


class PostCommitExecutor:
    """Admit at most 64 running + queued mirror chains without waiting.

    A normal settle has about three serial RPCs. Four workers cap concurrent
    optional RPCs at four; 64 slots absorb short bursts without accumulating
    unbounded payloads when those RPCs stall. Overflow is deliberately lossy.
    """

    def __init__(self) -> None:
        self.executor = ThreadPoolExecutor(
            max_workers=WORKERS, thread_name_prefix="settle-post-commit",
        )
        self._slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)
        self._lock = threading.Lock()
        self.in_flight = 0
        self.drops: Counter[str] = Counter()
        self._last_drop_log: dict[str, float] = {}

    def _drop(self, kind: str) -> None:
        now = time.monotonic()
        with self._lock:
            self.drops[kind] += 1
            count = self.drops[kind]
            emit = now - self._last_drop_log.get(kind, float("-inf")) >= DROP_LOG_INTERVAL_SECONDS
            if emit:
                self._last_drop_log[kind] = now
        if emit:
            log.warning("post_commit_dropped kind=%s dropped_total=%d", kind, count)

    def submit(self, task: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        kind = task.__name__
        if not self._slots.acquire(blocking=False):
            self._drop(kind)
            return
        with self._lock:
            self.in_flight += 1

        def run() -> None:
            try:
                # Callers retain their task-specific outer exception guards.
                task(*args, **kwargs)
            finally:
                self._release()

        try:
            self.executor.submit(run)
        except Exception:
            self._release()
            self._drop(kind)
            log.exception("post_commit_submission_failed kind=%s", kind)

    def _release(self) -> None:
        with self._lock:
            self.in_flight -= 1
        self._slots.release()


POST_COMMIT = PostCommitExecutor()


def defer_post_commit(
    background_tasks: BackgroundTasks, task: Callable[..., None], *args: Any, **kwargs: Any,
) -> None:
    # Starlette invokes async tasks directly after sending the body. Only the
    # O(1), non-waiting admission runs here; RPCs never run on the event loop
    # or through run_in_threadpool / AnyIO's shared capacity limiter.
    async def submit() -> None:
        POST_COMMIT.submit(task, *args, **kwargs)

    background_tasks.add_task(submit)
