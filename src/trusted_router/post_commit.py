"""Lossy, bounded post-response mirrors, isolated from billing's AnyIO pool."""

from __future__ import annotations

import functools
import logging
import threading
import time
from collections import Counter, deque
from collections.abc import Callable
from typing import Any

from starlette.background import BackgroundTasks

log = logging.getLogger(__name__)
WORKERS = 4
MAX_IN_FLIGHT = 64
DROP_LOG_INTERVAL_SECONDS = 60.0


class PostCommitExecutor:
    """Admit at most 64 running + queued mirror chains without waiting.

    Four daemon workers cap optional RPC concurrency without borrowing billing
    tokens or holding up interpreter exit / atexit log flushes. Shutdown drops
    queued work and abandons active chains; optional mirrors are repairable.
    """

    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._queue: deque[tuple[str, Callable[[], None]]] = deque()
        self._workers: list[threading.Thread] = []
        self._closed = False
        self.in_flight = 0
        self.drops: Counter[str] = Counter()
        self._last_drop_log: dict[str, float] = {}

    def _drop(self, kind: str) -> None:
        # Called under the admission lock, including by the inline test executor.
        now = time.monotonic()
        self.drops[kind] += 1
        count = self.drops[kind]
        if now - self._last_drop_log.get(kind, float("-inf")) >= DROP_LOG_INTERVAL_SECONDS:
            self._last_drop_log[kind] = now
            log.warning("post_commit_dropped kind=%s dropped_total=%d", kind, count)

    def submit(self, task: Callable[..., None], *args: Any, **kwargs: Any) -> None:
        kind = task.__name__
        with self._condition:
            if self._closed or self.in_flight >= MAX_IN_FLIGHT:
                self._drop(kind)
                return
            self.in_flight += 1
            try:
                self._dispatch(kind, functools.partial(task, *args, **kwargs))
            except Exception:
                self._release()
                self._drop(kind)
                log.exception("post_commit_submission_failed kind=%s", kind)

    def _dispatch(self, kind: str, task: Callable[[], None]) -> None:
        # Start lazily so importing the module does not create threads. Admission,
        # enqueue, dequeue and close share one lock: no work can sneak past close.
        while len(self._workers) < WORKERS:
            worker = threading.Thread(
                target=self._worker, name=f"settle-post-commit-{len(self._workers)}",
                daemon=True,
            )
            worker.start()
            self._workers.append(worker)
        self._queue.append((kind, task))
        self._condition.notify()

    def _worker(self) -> None:
        while True:
            with self._condition:
                self._condition.wait_for(lambda: self._closed or bool(self._queue))
                if self._closed:
                    return
                kind, task = self._queue.popleft()
            self._run(kind, task)

    def _run(self, kind: str, task: Callable[[], None]) -> None:
        try:
            task()
        except Exception:
            # Retain the worker even if a caller's task-specific guard fails.
            log.exception("post_commit_task_failed kind=%s", kind)
        finally:
            self._release()

    def _release(self) -> None:
        with self._condition:
            self.in_flight -= 1
            self._condition.notify_all()

    def close(self) -> None:
        """Stop admission and discard queued work; never join active workers."""
        with self._condition:
            if self._closed:
                return
            self._closed = True
            queued = len(self._queue)
            self.drops.update(kind for kind, _ in self._queue)
            self._queue.clear()
            self.in_flight -= queued
            self._condition.notify_all()
            log.warning(
                "post_commit_closed queued_dropped=%d active_abandoned=%d dropped_total=%d",
                queued, self.in_flight, sum(self.drops.values()),
            )

    def wait_idle(self, timeout: float | None = 30.0) -> bool:
        """Completion barrier for tests, never used by application shutdown."""
        with self._condition:
            return self._condition.wait_for(lambda: self.in_flight == 0, timeout=timeout)


POST_COMMIT = PostCommitExecutor()


def close_post_commit() -> None:
    POST_COMMIT.close()


def defer_post_commit(
    background_tasks: BackgroundTasks, task: Callable[..., None], *args: Any, **kwargs: Any,
) -> None:
    # Starlette invokes async tasks directly after sending the body. Only the
    # O(1), non-waiting admission runs here; RPCs never run on the event loop
    # or through run_in_threadpool / AnyIO's shared capacity limiter.
    async def submit() -> None:
        POST_COMMIT.submit(task, *args, **kwargs)

    background_tasks.add_task(submit)
