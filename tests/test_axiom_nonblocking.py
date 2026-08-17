"""Axiom log shipping must not block the thread that logs.

THE DEFECT THIS GUARDS. `axiom_py.logging.AxiomHandler.emit()` flushes
SYNCHRONOUSLY in the calling thread whenever more than its interval (1 second)
has elapsed since the last flush:

    if len(self.buffer) >= 1000 or time.monotonic() - self.last_flush > self.interval:
        self.flush()

At low request volume more than a second has almost always elapsed, so a
request that emits one log line pays a blocking HTTPS POST to Axiom before it
can respond. Measured 2026-08-17: 551 ms for a cold POST to api.axiom.co.
Production console pages served from europe-west4 measured p50 1540 ms and this
was one of the summands.

The fix routes records through a bounded queue to a listener thread. These
tests pin the two properties that make the fix correct rather than merely
faster:

  1. emitting returns promptly even when shipping is slow, AND the record
     still arrives at the shipper (fast-and-lossy is not the goal);
  2. the PII scrub filter runs BEFORE the record enters the queue, so an
     unscrubbed record never exists in the queue.

Scope limit: these tests exercise the handler wiring directly rather than
`init_axiom`, because `init_axiom` returns early under pytest by design
(`_running_under_pytest`). What they do not cover is the real axiom-py client;
the blocking behaviour above is quoted from the vendored source, not asserted
here.
"""

from __future__ import annotations

import logging
import logging.handlers
import queue
import threading
import time

from trusted_router.axiom_config import (
    _DroppingQueueHandler,
    build_axiom_pipeline,
)

CANARY = "sk-tr-v1-BLOCKINGCANARY"


class _SlowShipper(logging.Handler):
    """Stands in for axiom-py's synchronous flush."""

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self.received: list[logging.LogRecord] = []
        self.arrived = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        time.sleep(self.delay)
        self.received.append(record)
        self.arrived.set()


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord(
        name="trusted_router.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_emitting_does_not_wait_for_the_shipper() -> None:
    """The property the whole change exists for."""
    shipper = _SlowShipper(delay=0.5)
    handler = _DroppingQueueHandler(queue.Queue(maxsize=100))
    listener = logging.handlers.QueueListener(handler.queue, shipper)
    listener.start()
    try:
        started = time.monotonic()
        handler.handle(_record("event happened"))
        elapsed = time.monotonic() - started

        assert elapsed < 0.05, f"emit blocked for {elapsed:.3f}s; shipping must be off-thread"
        assert shipper.arrived.wait(timeout=5), "record never reached the shipper"
        assert len(shipper.received) == 1
    finally:
        listener.stop()


def test_the_scrub_filter_runs_before_the_queue() -> None:
    """An unscrubbed record must never exist in the queue.

    This drives the REAL wiring via `build_axiom_pipeline`. An earlier version
    of this test built its own handler and attached the filter itself, so it
    passed even when the production wiring put the scrub filter on the shipper
    — i.e. it asserted a stdlib property (filters run before emit) instead of
    the security property (production scrubs before the queue). Moving the
    filter in the source must redden this test, and now does.
    """
    handler, listener = build_axiom_pipeline(_SlowShipper(delay=0), resolved_level=logging.INFO)

    handler.handle(_record("rotating a key", context={"api_key": CANARY}))

    queued = handler.queue.get_nowait()
    assert CANARY not in str(queued.__dict__), "unscrubbed record reached the queue"
    assert not listener._thread, "build must return the listener unstarted"


def test_the_pipeline_puts_the_scrub_filter_on_the_queue_handler() -> None:
    """The placement, asserted directly, so the reason is legible in a failure."""
    handler, _ = build_axiom_pipeline(_SlowShipper(delay=0), resolved_level=logging.INFO)

    filter_names = {type(f).__name__ for f in handler.filters}
    assert "_AxiomScrubFilter" in filter_names, (
        "the scrub filter must sit on the queue handler; on the shipper it would "
        "run after the record has already crossed into the queue"
    )


def test_a_full_queue_drops_instead_of_blocking() -> None:
    """Backpressure must cost observability, never latency."""
    handler = _DroppingQueueHandler(queue.Queue(maxsize=1))
    handler.handle(_record("first"))

    started = time.monotonic()
    for index in range(50):
        handler.handle(_record(f"overflow {index}"))
    elapsed = time.monotonic() - started

    assert elapsed < 0.5, f"a full queue blocked for {elapsed:.3f}s"
    assert handler._dropped >= 50, "drops were not counted"


def test_the_idempotency_guard_knows_the_handler_that_is_actually_attached() -> None:
    """`init_axiom` attaches `_DroppingQueueHandler` to root now. If the guard
    does not recognise it, a second `init_axiom` call starts a second listener
    thread and a second queue rather than short-circuiting."""
    from trusted_router.axiom_config import _handler_already_installed

    root = logging.getLogger()
    handler = _DroppingQueueHandler(queue.Queue(maxsize=1))
    root.addHandler(handler)
    try:
        assert _handler_already_installed() is True
    finally:
        root.removeHandler(handler)
