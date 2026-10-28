"""Dedicated warm settle-drain surface; dormant unless its own fast flag is on.

One leader-region process, never a loop on router instances. Claims run inside
available executor slots, never a queue. Health publication follows completed
scan/apply progress, including inside busy passes; a wedged loop cannot renew
admission evidence. Deployment is separate from this code change.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from trusted_router.config import Settings
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.services.settle_outbox_apply import ApplyOutcome
from trusted_router.storage_gcp_async_admission import (
    claim_health_publish,
    claim_housekeeping,
    publish_health,
)
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox

logger = logging.getLogger(__name__)

# Reused apply/resolve/reap helpers log identifiers on their legacy path. Keep
# those diagnostics out of warm-worker logs without changing the primitives or
# suppressing their severity. The context is local to the calling thread/task;
# router/watchdog log records are byte-for-byte untouched.
_primitive_log_scope: ContextVar[bool] = ContextVar("settle_drain_primitive_logs", default=False)
_log_factory_lock = threading.Lock()
_log_factory_users = 0
_original_log_factory: Callable[..., logging.LogRecord] = logging.getLogRecordFactory()
_worker_log_factory: Callable[..., logging.LogRecord] | None = None


@contextmanager
def _aggregate_primitive_logs() -> Iterator[None]:
    global _log_factory_users, _original_log_factory, _worker_log_factory
    with _log_factory_lock:
        if _log_factory_users == 0:
            previous = logging.getLogRecordFactory()
            def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
                record = previous(*args, **kwargs)
                if _primitive_log_scope.get() and record.name != __name__:
                    record.msg = "async_drain.primitive logger=%s severity=%s"
                    record.args = (record.name, record.levelname)
                    record.exc_info = record.exc_text = record.stack_info = None
                return record
            _original_log_factory, _worker_log_factory = previous, factory
            logging.setLogRecordFactory(factory)
        _log_factory_users += 1
    token = _primitive_log_scope.set(True)
    try:
        yield
    finally:
        _primitive_log_scope.reset(token)
        with _log_factory_lock:
            _log_factory_users -= 1
            if _log_factory_users == 0 and logging.getLogRecordFactory() is _worker_log_factory:
                logging.setLogRecordFactory(_original_log_factory)


class _ObservedOutbox(SpannerSettleOutbox):
    """Observe existing fenced return values without changing resolution SQL."""
    resolution: str | None = None

    def mark(self, *args: Any, **kwargs: Any) -> str | None:
        result = super().mark(*args, **kwargs)
        self.resolution = result
        logger.info("async_drain.resolution status=%s fence_miss=%s", result, int(result is None))
        return result

    def park(self, *args: Any, **kwargs: Any) -> bool:
        result = super().park(*args, **kwargs)
        self.resolution = "park" if result else None
        logger.info("async_drain.resolution status=park parked=%s fence_miss=%s", int(result), int(not result))
        return result


def _apply_slot(outbox: SpannerSettleOutbox, shard: int, lease: int,
                deadline: float, stop: threading.Event | None = None) -> tuple[int, str, int]:
    with _aggregate_primitive_logs():
        return _apply_slot_inner(outbox, shard, lease, deadline, stop)


def _apply_slot_inner(outbox: SpannerSettleOutbox, shard: int, lease: int,
                      deadline: float, stop: threading.Event | None) -> tuple[int, str, int]:
    # The slot has already started. There is no claimed executor queue tail.
    # Stop prevents new claims and new applies; already running applies finish.
    if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
        return 0, "", 0
    rows = outbox.claim_shard(shard=shard, lease_seconds=lease)
    if not rows:
        return 0, "", 0
    row = rows[0]
    # Claim RPCs can consume the lease/budget. Never start expired work, even
    # though the reservation fence would still prevent double billing.
    now = dt.datetime.now(dt.UTC)
    if ((stop is not None and stop.is_set())
            or time.monotonic() >= deadline or row.leased_until is None
            or dt.datetime.fromisoformat(row.leased_until.replace('Z', '+00:00')) <= now):
        return 1, "deferred", 0
    started = time.monotonic()
    error_note = None
    apply_error = None
    try:
        outcome = drain.apply_frozen_settle(row)
    except Exception as exc:
        outcome, apply_error = ApplyOutcome.ERROR, exc
        error_note = f"{type(exc).__name__}: {exc}"
    observed = _ObservedOutbox(outbox._database, outbox._pt, async_fence=outbox._async_fence)
    try:
        drain._resolve_row(observed, row, outcome, error_note=error_note, apply_error=apply_error)
    except Exception:
        outcome = "resolve_error"
    # Aggregate only: never emit authorization/key/workspace IDs here.
    if observed.resolution == "done" and row.created_at is not None:
        try:
            created = dt.datetime.fromisoformat(row.created_at.replace('Z', '+00:00'))
            latency = (dt.datetime.now(dt.UTC) - created).total_seconds()
            logger.info("async_drain.completed latency_seconds=%.6f", latency)
        except (ValueError, TypeError):
            logger.warning("async_drain.completion_timestamp_invalid")
    elapsed = time.monotonic() - started
    logger.info("async_drain.timing outcome=%s service_seconds=%.6f observed_at=%.6f",
                outcome, elapsed, time.time())
    return 1, outcome, int(row.actual_cost_micro) if outcome == ApplyOutcome.SETTLED_NOW else 0


@dataclass
class WorkerCadence:
    """Process-local due times, with durable cadence still arbitrating overlaps.

    Only the drain thread calls these methods. HTTP liveness reads progress;
    it never refreshes progress or publishes health.
    """

    next_health: float = 0
    next_housekeeping: float = 0
    next_summary: float = 0
    progress_at: float = field(default_factory=time.monotonic)
    passes: int = 0
    claimed: int = 0

    def progressed(self, database: Any, settings: Settings, stop: threading.Event | None) -> None:
        self.progress_at = time.monotonic()
        if stop is not None and stop.is_set():
            return
        if self.progress_at < self.next_health:
            return
        interval = settings.settle_outbox_health_publish_interval_seconds
        self.next_health = self.progress_at + interval
        try:
            if claim_health_publish(database, interval):
                publish_health(database)
        except Exception:
            # Failed/slow publication ages out; never manufacture evidence.
            logger.warning("async_drain.health_publish_failed")

    def housekeeping(self, outbox: SpannerSettleOutbox, settings: Settings,
                     stop: threading.Event | None) -> tuple[int, int]:
        now = time.monotonic()
        if now < self.next_housekeeping or (stop is not None and stop.is_set()):
            return 0, 0
        self.next_housekeeping = now + 300
        try:
            if claim_housekeeping(outbox._database):
                with _aggregate_primitive_logs():
                    return drain.housekeeping(outbox,
                        reap_snapshot_booking_enabled=settings.reap_snapshot_booking_enabled)
        except Exception:
            logger.warning("async_drain.housekeeping_failed")
        return 0, 0


def drain_pass(limit: int, *, settings: Settings, start_shard: int = 0,
               stop: threading.Event | None = None,
               cadence: WorkerCadence | None = None) -> dict[str, Any]:
    if not settings.settle_outbox_fast_drain_enabled:
        return drain.drain_settle_outbox(limit,
            reap_snapshot_booking_enabled=settings.reap_snapshot_booking_enabled)
    cadence = cadence or WorkerCadence()
    budget, lease = settings.settle_outbox_pass_budget_seconds, settings.settle_outbox_lease_seconds
    if not 0 < budget < lease:
        raise ValueError("fast drain budget must be below lease")
    outbox = drain.spanner_settle_outbox()
    limit = max(1, min(int(limit), 500))
    concurrency = min(settings.settle_outbox_worker_concurrency, settings.settle_outbox_claim_batch)
    if not 1 <= concurrency <= 32:
        raise ValueError("fast drain concurrency outside bound")
    started = time.monotonic()
    deadline = started + budget
    claimed = recovered = empty = 0
    outcomes: Counter[str] = Counter()
    shard = start_shard % 16
    with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="settle-drain") as pool:
        while (claimed < limit and time.monotonic() < deadline and empty < 16
               and not (stop is not None and stop.is_set())):
            # At most one row per running slot, even when claim_batch is large.
            width = min(concurrency, limit - claimed)
            futures = [pool.submit(_apply_slot, outbox, (shard + i) % 16, lease, deadline, stop)
                       for i in range(width)]
            shard = (shard + width) % 16
            for future in as_completed(futures):
                count, outcome, amount = future.result()
                cadence.progressed(outbox._database, settings, stop)
                claimed += count
                recovered += amount
                empty = 0 if count else empty + 1
                if outcome:
                    outcomes[outcome] += 1
    purged, reaped = cadence.housekeeping(outbox, settings, stop)
    cadence.passes += 1
    cadence.claimed += claimed
    if claimed or outcomes or purged or reaped:
        logger.info("async_drain.pass claimed=%s outcomes=%s elapsed_seconds=%.6f",
                    claimed, dict(outcomes), time.monotonic() - started)
    if time.monotonic() >= cadence.next_summary:
        logger.info("async_drain.summary passes=%s claimed=%s", cadence.passes, cadence.claimed)
        cadence.passes = cadence.claimed = 0
        cadence.next_summary = time.monotonic() + 60
    return dict(claimed=claimed, outcomes=dict(outcomes), recovered_micro=recovered,
                purged=purged, reaped=reaped, deferred=outcomes.get("deferred", 0))


def run_worker(settings: Settings, stop: threading.Event, *, limit: int = 500,
               cadence: WorkerCadence | None = None) -> None:
    """Callable warm-worker loop; stop interrupts polling, not an in-flight apply.

    Existing Spanner apply RPC/retry bounds still apply. A pass budget prevents
    new starts, not cancellation of an already running money transaction.
    """
    if not settings.settle_outbox_fast_drain_enabled:
        logger.info("async_drain.idle fast mode disabled")
        return
    cadence = cadence or WorkerCadence()
    shard = 0
    while not stop.is_set():
        started = time.monotonic()
        try:
            drain_pass(limit, settings=settings, start_shard=shard, stop=stop, cadence=cadence)
        except Exception:
            logger.warning("async_drain.pass_failed")
            # An exception proves the loop is responsive, but does NOT publish.
            cadence.progress_at = time.monotonic()
        shard = (shard + 1) % 16
        stop.wait(max(0., settings.settle_outbox_poll_interval_seconds - (time.monotonic() - started)))


SHUTDOWN_JOIN_SECONDS = 8.0
LIVENESS_STALL_SECONDS = 60.0


def create_worker_app(settings: Settings) -> FastAPI:
    """No auth routes, middleware, telemetry clients or unrelated starters."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.state.settings = settings
    stop = threading.Event()
    cadence = WorkerCadence()
    thread: threading.Thread | None = None

    @app.on_event("startup")
    async def _start_settle_drain() -> None:
        nonlocal thread
        if not settings.settle_outbox_fast_drain_enabled:
            logger.info("async_drain.idle fast mode disabled")
            return
        cadence.progress_at = time.monotonic()
        thread = threading.Thread(target=run_worker, args=(settings, stop),
                                  kwargs={"cadence": cadence}, name="settle-drain-loop", daemon=True)
        thread.start()

    @app.on_event("shutdown")
    async def _stop_settle_drain() -> None:
        stop.set()
        if thread is not None:
            # Bound the lifecycle wait below Cloud Run's SIGTERM grace. Active
            # money RPCs keep their existing deadlines/fences; SIGKILL may
            # abandon them and their rows become reclaimable at lease expiry.
            thread.join(SHUTDOWN_JOIN_SECONDS)
            if thread.is_alive():
                logger.warning("async_drain.shutdown_inflight")

    @app.get("/health")
    async def health() -> JSONResponse:
        idle = not settings.settle_outbox_fast_drain_enabled
        responsive = (thread is not None and thread.is_alive()
                      and time.monotonic() - cadence.progress_at < LIVENESS_STALL_SECONDS)
        healthy = idle or responsive
        return JSONResponse({"status": "idle" if idle else "ok" if healthy else "stalled"},
                            status_code=200 if healthy else 503)

    return app
