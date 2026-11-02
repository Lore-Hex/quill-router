"""Content-free, best-effort observations of the serial async-settle worker.

Admission subphases overlap admission_ms; transaction subphases overlap
enqueue_ms. A copied context on another thread cannot contribute observations.
"""
from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from fractions import Fraction
from functools import wraps
from time import monotonic
from typing import Any, ParamSpec, TypeVar

P = ParamSpec("P")
T = TypeVar("T")
logger = logging.getLogger(__name__)
OUTCOMES = frozenset({"accepted", "duplicate", "sync_required", "rejected", "unknown"})
ADMISSION_FIELDS = ("admission_ms", "lock_wait_ms", "health", "health_ms", "workspace", "workspace_ms")
TOP_LEVEL = ("auth_ms", "body_ms", "queue_ms", "validate_ms", "admission_ms", "enqueue_ms")
READ_OUTCOMES = frozenset({"none", "read", "error"})
FIELDS = ("auth_ms", "body_ms", "queue_ms", "validate_ms", *ADMISSION_FIELDS,
          "enqueue_ms", "callback_ms", "commit_ms", "attempts", "unclassified_ms", "total_ms")


def best_effort(fn: Callable[P, None]) -> Callable[P, None]:
    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> None:
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: S110 - logging failures cannot be logged safely
            pass  # Instrumentation must never affect admission or settlement.
    return wrapped


def stamp() -> float | None:
    try:
        value = monotonic()
        return value if math.isfinite(value) else None
    except Exception:
        return None


class Timing:
    def __init__(self, started: float, body_finished: float | None = None,
                 authenticated: float | None = None) -> None:
        self.started = started
        self.body_finished = body_finished
        self.authenticated = authenticated
        self.owner = threading.get_ident()
        self.active = True
        self.admitting = False
        self.values = dict.fromkeys((name for name in FIELDS if name not in {"health", "workspace"}), Fraction(0))
        self.reads = {"health": "none", "workspace": "none"}
        self.outcome = "rejected"
        self.validating: float | None = None
        self.committing: float | None = None
        self.total_saved = False

    @best_effort
    def add(self, field: str, start: float | None, end: float | None) -> None:
        if start is not None and end is not None:
            self.values[field] += (Fraction(end) - Fraction(start)) * 1000

    @best_effort
    def worker(self, entered: float | None) -> None:
        self.owner = threading.get_ident()
        if self.body_finished is not None:
            self.add("auth_ms", self.started, self.authenticated)
            self.add("body_ms", self.authenticated, self.body_finished)
            self.add("queue_ms", self.body_finished, entered)
        self.validating = entered

    @best_effort
    def stop_validation(self) -> None:
        self.add("validate_ms", self.validating, stamp())
        self.validating = None

    @best_effort
    def end_commit(self) -> None:
        self.add("commit_ms", self.committing, stamp())
        self.committing = None

    @best_effort
    def emit(self, *, projection: bool = False) -> None:
        if not projection:
            # Top-level intervals are disjoint and end before the total sample.
            # Exact differences of raw binary clock readings avoid rounding drift
            # even with zero gaps. Never clamp or redistribute the remainder.
            self.values["unclassified_ms"] = self.values["total_ms"] - sum(
                (self.values[name] for name in TOP_LEVEL), Fraction(0))
        fields = ADMISSION_FIELDS if projection else FIELDS
        prefix = "metric=async_settle_projection_admission" if projection else (
            "metric=async_settle_handoff_phases outcome=" + (self.outcome if self.outcome in OUTCOMES else "rejected"))
        # Only locally defined keys and numeric values reach the logger.
        parts = [prefix]
        for name in fields:
            if name in self.reads:
                label = self.reads[name]
                parts.append(f"{name}={label if label in READ_OUTCOMES else 'none'}")
                continue
            value = self.values[name]
            if name == "unclassified_ms":
                assert math.isfinite(value) and value >= 0
            elif not math.isfinite(value) or value < 0:
                value = Fraction(0)
            parts.append(f"{name}={int(value)}" if name == "attempts"
                         else f"{name}={float(value):.3f}")
        logger.info(" ".join(parts))


_CURRENT: ContextVar[Timing | None] = ContextVar("async_settle_timing", default=None)


def current() -> Timing | None:
    timing = _CURRENT.get()
    return timing if timing is not None and timing.active and timing.owner == threading.get_ident() else None


@contextmanager
def route_scope(started: float, authenticated: float | None, *, enabled: bool) -> Iterator[None]:
    token = None
    try:
        if enabled:
            token = _CURRENT.set(Timing(started, stamp(), authenticated))
    except Exception:  # noqa: S110 - best-effort scope setup
        pass
    try:
        yield
    finally:
        if token is not None:
            _CURRENT.reset(token)


@contextmanager
def handoff_scope(started: float, entered: float | None, *, enabled: bool) -> Iterator[None]:
    timing = None
    token = None
    try:
        if enabled:
            # The route deliberately hands this object to its designated worker.
            timing = _CURRENT.get() or Timing(started)
            timing.worker(entered)
        token = _CURRENT.set(timing)
    except Exception:  # noqa: S110 - best-effort scope setup
        pass
    try:
        yield
    except BaseException:
        if timing is not None and timing.outcome != "unknown":
            timing.outcome = "rejected"
        raise
    finally:
        if timing is not None:
            timing.stop_validation()
            if not timing.total_saved:
                timing.add("total_ms", started, stamp())
            timing.active = False
            timing.emit()
        if token is not None:
            _CURRENT.reset(token)


@best_effort
def stop_validation() -> None:
    timing = current()
    if timing is not None:
        timing.stop_validation()


@best_effort
def outcome(label: str) -> None:
    timing = current()
    if timing is not None and label in OUTCOMES:
        timing.stop_validation()
        timing.outcome = label


@best_effort
def save_total(finished: float) -> None:
    timing = current()
    if timing is not None:
        timing.add("total_ms", timing.started, finished)
        timing.total_saved = True


@contextmanager
def measure(field: str, *, admission_only: bool = False) -> Iterator[None]:
    timing = current()
    if timing is not None and admission_only and not timing.admitting:
        timing = None
    start = stamp() if timing is not None else None
    try:
        yield
    finally:
        if timing is not None:
            timing.add(field, start, stamp())


def read(fn: Callable[P, T], field: str) -> Callable[P, T]:
    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        timing = current()
        if timing is not None and not timing.admitting:
            timing = None
        try:
            with measure(field + "_ms", admission_only=True):
                result = fn(*args, **kwargs)
        except BaseException:
            if timing is not None:
                timing.reads[field] = "error"
            raise
        else:
            if timing is not None:
                timing.reads[field] = "read"
            return result
    return wrapped


class AdmissionLock:
    """Delegate the exact acquire/release calls; observe only eligible's scope."""
    def __init__(self, lock: Any) -> None:
        self.lock = lock

    def acquire(self, *args: Any, **kwargs: Any) -> Any:
        with measure("lock_wait_ms", admission_only=True):
            return self.lock.acquire(*args, **kwargs)

    def release(self) -> None:
        self.lock.release()

    def locked(self) -> bool:
        return bool(self.lock.locked())

    def __enter__(self) -> Any:
        return self.acquire()

    def __exit__(self, *args: Any) -> None:
        self.release()


def eligible(cache: Any, workspace: str, cap: int, *, projection: bool = False) -> bool:
    timing = current()
    token = None
    if projection:
        try:
            timing = Timing(0)
            token = _CURRENT.set(timing)
        except Exception:
            timing = None
    if timing is not None:
        timing.stop_validation()
        timing.admitting = True
    try:
        with measure("admission_ms"):
            return bool(cache.eligible(workspace, cap))
    finally:
        if timing is not None:
            timing.admitting = False
            if projection:
                timing.active = False
                timing.emit(projection=True)
        if token is not None:
            _CURRENT.reset(token)


def callback(fn: Callable[P, T]) -> Callable[P, T]:
    """Observe callback entry/exit without inspecting or replacing SDK arguments."""
    @wraps(fn)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> T:
        observation = current()
        entered = stamp() if observation is not None else None
        if observation is not None:
            observation.add("commit_ms", observation.committing, entered)
            observation.committing = None
            observation.values["attempts"] += 1
        succeeded = False
        try:
            result = fn(*args, **kwargs)
            succeeded = True
            return result
        finally:
            if observation is not None:
                finished = stamp()
                observation.add("callback_ms", entered, finished)
                if succeeded:
                    observation.committing = finished
    return wrapped


@contextmanager
def transaction_run() -> Iterator[None]:
    try:
        yield
    finally:
        timing = current()
        if timing is not None:
            timing.end_commit()
