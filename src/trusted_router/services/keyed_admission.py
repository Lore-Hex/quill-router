"""Bounded per-subject concurrency admission for expensive internal work."""

from __future__ import annotations

import threading


class KeyedConcurrencyAdmission:
    """Keep one hot subject from occupying every worker in a process.

    Entries exist only while work is in flight, so memory is bounded by both
    ``max_subjects`` and the server's own request concurrency. Rejection is
    immediate: callers should retry with a new request after the advertised
    backoff rather than queueing work in application memory.
    """

    def __init__(self, *, max_subjects: int = 10_000) -> None:
        if max_subjects <= 0:
            raise ValueError("max_subjects must be positive")
        self._max_subjects = max_subjects
        self._lock = threading.Lock()
        self._in_flight: dict[str, int] = {}

    def try_acquire(self, subject: str, *, limit: int) -> bool:
        if not subject:
            raise ValueError("subject must not be empty")
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._lock:
            current = self._in_flight.get(subject)
            if current is None:
                if len(self._in_flight) >= self._max_subjects:
                    return False
                self._in_flight[subject] = 1
                return True
            if current >= limit:
                return False
            self._in_flight[subject] = current + 1
            return True

    def release(self, subject: str) -> None:
        with self._lock:
            current = self._in_flight.get(subject)
            if current is None:
                return
            if current == 1:
                self._in_flight.pop(subject, None)
            else:
                self._in_flight[subject] = current - 1

    def count(self, subject: str) -> int:
        """Return a subject's live count for diagnostics and deterministic tests."""
        with self._lock:
            return self._in_flight.get(subject, 0)
