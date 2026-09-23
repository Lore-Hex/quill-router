"""Bounded background delivery for non-authoritative spend-lease evidence.

Spend-lease shadow rows help validate a rollout, but they do not authorize,
reserve, settle, or refund money. A telemetry-store stall therefore must not
delay a paid gateway request. This dispatcher keeps delivery off the request
thread, retries the in-flight row, and drops the oldest queued evidence when
the bounded buffer fills so memory use remains fixed.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_MAX_PENDING = 4_096
DEFAULT_RETRY_SECONDS = 0.25
MAX_RETRY_SECONDS = 30.0
ERROR_AFTER_CONSECUTIVE_FAILURES = 4


@dataclass(frozen=True)
class SpendLeaseShadowDispatchStats:
    attempted: int = 0
    enqueued: int = 0
    submitted: int = 0
    delivered: int = 0
    dropped: int = 0
    failures: int = 0
    pending: int = 0


class SpendLeaseShadowDispatcher:
    """Deliver shadow evidence on one lazy daemon thread."""

    def __init__(
        self,
        deliver: Callable[[str, dict[str, Any]], None],
        *,
        max_pending: int = DEFAULT_MAX_PENDING,
        retry_seconds: float = DEFAULT_RETRY_SECONDS,
    ) -> None:
        if max_pending <= 0:
            raise ValueError("spend-lease shadow queue size must be positive")
        if retry_seconds <= 0:
            raise ValueError("spend-lease shadow retry delay must be positive")
        self._deliver = deliver
        self._max_pending = max_pending
        self._retry_seconds = retry_seconds
        self._pending: deque[tuple[str, dict[str, Any]]] = deque(maxlen=max_pending)
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._active = False
        self._closed = False
        self._attempted = 0
        self._instance_id = uuid.uuid4().hex
        self._last_stats_log = 0.0
        self._submitted = 0
        self._delivered = 0
        self._dropped = 0
        self._failures = 0

    def submit(self, event_id: str, payload: dict[str, Any]) -> None:
        dropped_total: int | None = None
        with self._condition:
            self._attempted += 1
            if self._closed:
                self._dropped += 1
                self._log_stats_locked(force=True)
                raise RuntimeError("spend-lease shadow dispatcher is closed")
            if len(self._pending) == self._max_pending:
                self._dropped += 1
                dropped_total = self._dropped
            self._pending.append((event_id, payload))
            self._submitted += 1
            self._ensure_thread_locked()
            self._condition.notify()
            self._log_stats_locked(force=_should_log_count(dropped_total or 0))

        if dropped_total is not None and _should_log_count(dropped_total):
            log.error(
                "spend_lease_shadow_queue_overflow",
                extra={
                    "dropped_total": dropped_total,
                    "max_pending": self._max_pending,
                },
            )

    def stats(self) -> SpendLeaseShadowDispatchStats:
        with self._condition:
            return SpendLeaseShadowDispatchStats(
                attempted=self._attempted,
                enqueued=self._submitted,
                submitted=self._submitted,
                delivered=self._delivered,
                dropped=self._dropped,
                failures=self._failures,
                pending=len(self._pending) + int(self._active),
            )

    def _log_stats_locked(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_stats_log < 60:
            return
        self._last_stats_log = now
        log.info(
            "spend_lease_shadow_dispatch_counts dispatcher_id=%s attempted=%s "
            "enqueued=%s dropped=%s delivered=%s pending=%s failures=%s",
            self._instance_id, self._attempted, self._submitted, self._dropped,
            self._delivered, len(self._pending) + int(self._active), self._failures,
            extra={
                "dispatcher_id": self._instance_id,
                "attempted": self._attempted,
                "enqueued": self._submitted,
                "dropped": self._dropped,
                "delivered": self._delivered,
                "pending": len(self._pending) + int(self._active),
                "failures": self._failures,
            },
        )

    def wait_for_idle(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while self._pending or self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def close(self, timeout: float = 1.0) -> None:
        with self._condition:
            self._closed = True
            self._log_stats_locked(force=True)
            self._condition.notify_all()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _ensure_thread_locked(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="spend-lease-shadow",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        current: tuple[str, dict[str, Any]] | None = None
        consecutive_failures = 0
        while True:
            if current is None:
                with self._condition:
                    while not self._pending and not self._closed:
                        self._condition.wait()
                    if self._closed and not self._pending:
                        return
                    current = self._pending.popleft()
                    self._active = True

            event_id, payload = current
            try:
                self._deliver(event_id, payload)
            except Exception as exc:  # noqa: BLE001 - telemetry must not kill worker
                consecutive_failures += 1
                with self._condition:
                    self._failures += 1
                    queued = len(self._pending)
                    failures = self._failures
                if consecutive_failures == 1:
                    log.warning(
                        "spend_lease_shadow_delivery_failed",
                        extra={"error_class": type(exc).__name__, "queued": queued},
                    )
                elif (
                    consecutive_failures >= ERROR_AFTER_CONSECUTIVE_FAILURES
                    and _should_log_count(consecutive_failures)
                ):
                    log.error(
                        "spend_lease_shadow_delivery_persistently_failing",
                        exc_info=True,
                        extra={
                            "consecutive_failures": consecutive_failures,
                            "error_class": type(exc).__name__,
                            "failure_total": failures,
                            "queued": queued,
                        },
                    )
                elif _should_log_count(consecutive_failures):
                    log.warning(
                        "spend_lease_shadow_delivery_still_failing",
                        extra={
                            "consecutive_failures": consecutive_failures,
                            "error_class": type(exc).__name__,
                            "failure_total": failures,
                            "queued": queued,
                        },
                    )
                delay = min(
                    self._retry_seconds * (2 ** min(consecutive_failures - 1, 16)),
                    MAX_RETRY_SECONDS,
                )
                with self._condition:
                    self._condition.wait_for(lambda: self._closed, timeout=delay)
                    if self._closed:
                        self._dropped += 1 + len(self._pending)
                        self._pending.clear()
                        self._active = False
                        self._log_stats_locked(force=True)
                        self._condition.notify_all()
                        return
                continue

            with self._condition:
                self._delivered += 1
                self._active = False
                self._condition.notify_all()
            if consecutive_failures:
                log.info(
                    "spend_lease_shadow_delivery_recovered",
                    extra={"consecutive_failures": consecutive_failures},
                )
            consecutive_failures = 0
            current = None


def _should_log_count(value: int) -> bool:
    """Log the first event and powers of two without creating an alert flood."""

    return value > 0 and value & (value - 1) == 0
