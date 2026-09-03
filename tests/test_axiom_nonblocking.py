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
  2. queue preparation scrubs a private copy BEFORE it enters the queue, so an
     unscrubbed record never exists there and sibling handlers stay unchanged.

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
from collections.abc import Iterator, Mapping
from contextlib import suppress

import pytest

from trusted_router.axiom_config import (
    _DroppingQueueHandler,
    _make_axiom_shutdown,
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


class _BlockingShipper(logging.Handler):
    """Block shipping until the test explicitly allows it to finish."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release_event = threading.Event()
        self.finished = threading.Event()
        self.thread_id: int | None = None
        self.received: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.thread_id = threading.get_ident()
        self.started.set()
        self.release_event.wait()
        self.received.append(record)
        self.finished.set()


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
    shipper = _BlockingShipper()
    handler, listener = build_axiom_pipeline(shipper, resolved_level=logging.INFO)
    listener.start()
    worker = listener._thread
    assert worker is not None
    test_thread_id = threading.get_ident()
    emitting_thread_ids: list[int] = []
    emit_returned = threading.Event()

    def emit_record() -> None:
        emitting_thread_ids.append(threading.get_ident())
        handler.handle(_record("event happened"))
        emit_returned.set()

    emitter = threading.Thread(target=emit_record, daemon=True)
    emitter.start()
    try:
        assert shipper.started.wait(timeout=5), "record never reached the shipper"
        assert emit_returned.wait(timeout=5), "emit waited for the blocked shipper"
        assert not shipper.release_event.is_set(), "shipper was released before emit returned"
        assert not shipper.finished.is_set(), "emit waited for the blocked shipper"
        assert shipper.thread_id != test_thread_id, "shipping ran on the test thread"
        assert shipper.thread_id != emitting_thread_ids[0], "shipping ran on the emitting thread"
        assert shipper.thread_id == worker.ident
    finally:
        shipper.release_event.set()
        emitter.join(timeout=5)
        listener.enqueue_sentinel()
        worker.join(timeout=5)
        assert not emitter.is_alive(), "emitting thread did not stop"
        assert not worker.is_alive(), "shipper worker did not stop"

    assert shipper.finished.is_set()
    assert len(shipper.received) == 1


def test_scrubbing_runs_before_the_queue() -> None:
    """An unscrubbed record must never exist in the queue.

    This drives the REAL wiring via `build_axiom_pipeline`. An earlier version
    of this test built its own handler and attached the filter itself, so it
    passed even when the production wiring put the scrub filter on the shipper
    — i.e. it asserted a stdlib property (filters run before emit) instead of
    the security property (production scrubs before the queue). Moving the
    filter in the source must redden this test, and now does.
    """
    shipper = _SlowShipper(delay=0)
    handler, _ = build_axiom_pipeline(shipper, resolved_level=logging.INFO)

    handler.handle(_record("rotating a key", context={"api_key": CANARY}))

    queued = handler.queue.get_nowait()
    assert CANARY not in str(queued.__dict__), "unscrubbed record reached the queue"
    assert shipper.received == [], "build must return the listener unstarted"


def test_exception_and_stack_data_are_scrubbed_at_the_queue_boundary() -> None:
    """No exception, stack, or extra PII may cross the queue boundary.

    Axiom is the structured-log tier, while Sentry owns exception detail.
    Pattern matching is insufficient for arbitrary exception text, so inspect
    the exact queued copy and require the raw exception fields to be absent.
    """
    exception_canary = "sk-tr-v1-EXCEPTIONCANARY"
    arbitrary_exception = "ARBITRARY-PATIENT-NARRATIVE-74291"
    cached_exception = "CACHED-ARBITRARY-NARRATIVE-91357"
    stack_canary = "queue-stack-canary@example.com"
    top_level_email = "queue-extra-canary@example.com"
    nested_email = "queue-nested-canary@example.com"
    opaque_authorization = "Bearer opaque-authorization-canary"
    private_canary = "sk-tr-v1-PRIVATECANARY"
    opaque_api_key = "opaque-api-key-canary"
    field_name_canary = "sk-tr-v1-FIELDNAMECANARY"
    try:
        raise RuntimeError(f"upstream rejected {exception_canary} {arbitrary_exception}")
    except RuntimeError as exc:
        record = logging.LogRecord(
            name="trusted_router.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request failed",
            args=(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    record.stack_info = f"Stack (most recent call last):\n  account={stack_canary}"
    record.exc_text = f"already formatted: {cached_exception}"
    record.customer_email = top_level_email
    record.error_message = nested_email
    record.authorization = opaque_authorization
    record.api_key = opaque_api_key
    record._private = private_canary
    record.context = {
        "contact": nested_email,
        "items": ("token=opaque-nested-token", {"prompt": "private narrative"}),
    }
    setattr(record, field_name_canary, "field-name value")

    assert exception_canary in str(record.exc_info[1])
    assert stack_canary in record.stack_info
    original_payload = record.__dict__.copy()
    shipper = _SlowShipper(delay=0)
    handler, _ = build_axiom_pipeline(
        shipper,
        resolved_level=logging.INFO,
    )

    handler.handle(record)

    queued = handler.queue.get_nowait()
    queued_payload = repr(queued.__dict__)
    assert queued is not record
    for canary in (
        exception_canary,
        arbitrary_exception,
        cached_exception,
        stack_canary,
        top_level_email,
        nested_email,
        opaque_authorization,
        private_canary,
        opaque_api_key,
        field_name_canary,
    ):
        assert canary not in queued_payload
    assert queued.msg == queued.message
    assert queued.args is None
    assert queued.exc_info is None
    assert queued.exc_text is None
    assert queued.stack_info is None
    assert queued.customer_email == "[Filtered-email]"
    assert queued.error_message == "[Filtered-email]"
    assert queued.authorization == "[Filtered]"
    assert queued.api_key == "[Filtered]"
    assert queued._private == "[Filtered]"
    assert queued.context == {
        "contact": "[Filtered-email]",
        "items": ("token=[Filtered]", {"prompt": "[Filtered]"}),
    }
    assert queued.__dict__["[Filtered]"] == "field-name value"
    # The Axiom handler must sanitize its own copy. Other handlers attached to
    # the same logger should still receive the caller's original record.
    assert record.__dict__ == original_payload
    assert record.args == ()
    assert record.customer_email == top_level_email
    assert record.error_message == nested_email
    assert record.authorization == opaque_authorization
    assert record.api_key == opaque_api_key
    assert record._private == private_canary
    assert record.context == {
        "contact": nested_email,
        "items": ("token=opaque-nested-token", {"prompt": "private narrative"}),
    }
    assert record.exc_text == f"already formatted: {cached_exception}"
    assert record.stack_info == f"Stack (most recent call last):\n  account={stack_canary}"
    assert getattr(record, field_name_canary) == "field-name value"
    assert "message" not in record.__dict__
    assert shipper.received == [], "build must return the listener unstarted"


def test_the_pipeline_scrubs_a_copy_in_queue_prepare() -> None:
    """Scrubbing must happen before enqueue without mutating sibling handlers."""
    handler, _ = build_axiom_pipeline(_SlowShipper(delay=0), resolved_level=logging.INFO)

    filter_names = {type(f).__name__ for f in handler.filters}
    assert "_AxiomScrubFilter" not in filter_names, (
        "handler filters mutate the shared caller record; the queue handler "
        "must copy and scrub inside prepare() instead"
    )
    assert type(handler._queue_scrubber).__name__ == "_AxiomScrubFilter"


def test_actual_shipper_never_receives_raw_exception_stack_or_extra_data() -> None:
    """The sanitized queue record must stay sanitized at the final handler."""
    exception_canary = "sk-tr-v1-SINKEXCEPTIONCANARY"
    arbitrary_exception = "SINK-ARBITRARY-PATIENT-NARRATIVE-85109"
    stack_canary = "sink-stack-canary@example.com"
    extra_canary = "sink-extra-canary@example.com"
    opaque_authorization = "Bearer sink-opaque-authorization"
    formatter_canary = "sk-tr-v1-FORMATTERCANARY"
    try:
        raise RuntimeError(f"provider rejected {exception_canary} {arbitrary_exception}")
    except RuntimeError as exc:
        record = logging.LogRecord(
            name="trusted_router.test",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="request failed",
            args=(),
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    record.stack_info = f"Stack (most recent call last):\n  account={stack_canary}"
    record.customer_email = extra_canary
    record.authorization = opaque_authorization

    shipper = _SlowShipper(delay=0)
    handler, listener = build_axiom_pipeline(shipper, resolved_level=logging.INFO)
    handler.setFormatter(
        logging.Formatter(
            "%(levelname)s %(message)s customer_email=%(customer_email)s "
            f"authorization=%(authorization)s trailer={formatter_canary}"
        )
    )
    listener.start()
    try:
        handler.handle(record)
    finally:
        listener.stop()

    assert len(shipper.received) == 1
    shipped_payload = repr(shipper.received[0].__dict__)
    for canary in (
        exception_canary,
        arbitrary_exception,
        stack_canary,
        extra_canary,
        opaque_authorization,
        formatter_canary,
    ):
        assert canary not in shipped_payload


def test_custom_message_and_bad_args_cannot_escape_or_break_logging() -> None:
    class ExplodingMessage:
        def __str__(self) -> str:
            raise AssertionError("the scrubber must not invoke custom __str__")

    handler, _ = build_axiom_pipeline(_SlowShipper(delay=0), resolved_level=logging.INFO)

    custom = _record("placeholder")
    custom.msg = ExplodingMessage()
    custom.args = ("sk-tr-v1-CUSTOMARGCANARY",)
    handler.handle(custom)
    queued_custom = handler.queue.get_nowait()

    malformed = _record("two values: %s %s")
    malformed.args = ("sk-tr-v1-MALFORMEDARGCANARY",)
    handler.handle(malformed)
    queued_malformed = handler.queue.get_nowait()

    assert queued_custom.msg == "[Filtered-ExplodingMessage]"
    assert queued_custom.args is None
    assert "CUSTOMARGCANARY" not in repr(queued_custom.__dict__)
    assert queued_malformed.msg == "two values: %s %s"
    assert queued_malformed.args is None
    assert "MALFORMEDARGCANARY" not in repr(queued_malformed.__dict__)


def test_pathological_extras_drop_without_printing_the_raw_record(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Scrubber failures must not reach QueueHandler.handleError(original)."""

    class ExplodingItems(Mapping[object, object]):
        def __getitem__(self, key: object) -> object:
            raise KeyError(key)

        def __iter__(self) -> Iterator[object]:
            return iter(())

        def __len__(self) -> int:
            return 1

        def items(self) -> object:
            raise RuntimeError("mapping traversal failed")

    class ExplodingKey:
        def __str__(self) -> str:
            raise RuntimeError("key conversion failed")

    class ExplodingString(str):
        def lower(self) -> str:
            raise RuntimeError("string normalization failed")

    hostile_extras = (
        ExplodingItems(),
        {ExplodingKey(): "value"},
        ExplodingString("value"),
    )
    for index, hostile_extra in enumerate(hostile_extras):
        raw_canary = f"stderr-raw-{index}@example.com"
        handler, _ = build_axiom_pipeline(
            _SlowShipper(delay=0),
            resolved_level=logging.INFO,
        )
        record = _record("unsafe positional value: %s")
        record.args = (raw_canary,)
        record.context = hostile_extra

        handler.handle(record)

        captured = capsys.readouterr()
        assert handler.queue.empty()
        assert handler._dropped == 1
        assert "axiom.record_dropped dropped_total=1 reason=scrub_failed" in captured.err
        assert raw_canary not in captured.err
        assert "Message:" not in captured.err
        assert "Arguments:" not in captured.err


def test_shutdown_drains_then_flushes_once_and_is_safe_to_repeat(
    capsys: pytest.CaptureFixture[str],
) -> None:
    order: list[str] = []

    class BufferingShipper(logging.Handler):
        def __init__(self) -> None:
            super().__init__()
            self.buffer: list[str] = []
            self.flushed: list[str] = []
            self.flush_calls = 0

        def emit(self, record: logging.LogRecord) -> None:
            order.append("emit")
            self.buffer.append(record.getMessage())

        def flush(self) -> None:
            order.append("flush")
            self.flush_calls += 1
            self.flushed.extend(self.buffer)
            self.buffer.clear()

    shipper = BufferingShipper()
    handler, listener = build_axiom_pipeline(shipper, resolved_level=logging.INFO)
    shutdown = _make_axiom_shutdown(listener, shipper)
    listener.start()
    handler.handle(_record("queued before shutdown"))

    shutdown()
    shutdown()
    listener.stop()

    assert shipper.flushed == ["queued before shutdown"]
    assert shipper.buffer == []
    assert shipper.flush_calls == 1
    assert order == ["emit", "flush"]
    assert capsys.readouterr().err == ""


def test_a_full_queue_drops_instead_of_blocking() -> None:
    """Backpressure must cost observability, never latency."""
    handler = _DroppingQueueHandler(queue.Queue(maxsize=1))
    handler.handle(_record("first"))
    returned = threading.Event()

    def overflow() -> None:
        handler.handle(_record("overflow"))
        returned.set()

    caller = threading.Thread(target=overflow, daemon=True)
    caller.start()
    try:
        assert returned.wait(timeout=5), "a full queue waited for capacity"
        assert handler.queue.qsize() == 1, "overflow item was not dropped"
        assert handler._dropped == 1, "drop was not counted"
    finally:
        # Release a deliberately blocking mutation without leaking its thread;
        # an unexpectedly empty queue must not mask the assertion above.
        with suppress(queue.Empty):
            handler.queue.get_nowait()
        caller.join(timeout=5)

    assert not caller.is_alive()


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
