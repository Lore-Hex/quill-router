"""Run a handful of independent blocking reads at once.

The remediator and the /fleet heartbeat table each make several small,
independent analytics reads. Made one after another their latencies add up,
so one slow answer stretches the whole pass; made together the pass costs
the slowest read instead of the sum.
"""

from __future__ import annotations

import contextvars
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


def map_concurrently(
    fn: Callable[[T], R],
    items: Sequence[T],
    *,
    thread_name_prefix: str,
) -> list[Future[R]]:
    """Call ``fn`` on every item in its own thread and wait for all of them.

    Returns the finished futures in input order, so each caller decides how a
    failed item is handled (``future.result()`` re-raises it). Each thread
    runs in a copy of the caller's context, so context-local settings such as
    ``operational_analytics.read_timeout_cap`` reach the reads. Waits for
    every call: nothing is left running behind the caller's back, so callers
    that hold a single-flight admission keep it until the work has stopped.
    """
    if not items:
        return []
    if len(items) == 1:
        single: Future[R] = Future()
        try:
            single.set_result(fn(items[0]))
        except Exception as exc:  # noqa: BLE001 - surfaced via future.result()
            single.set_exception(exc)
        return [single]
    with ThreadPoolExecutor(
        max_workers=len(items),
        thread_name_prefix=thread_name_prefix,
    ) as pool:
        futures = [
            pool.submit(contextvars.copy_context().run, fn, item) for item in items
        ]
    return futures
