"""Dedicated warm settle-drain surface; dormant unless its own fast flag is on.

One leader-region process, never a loop on router instances. Claims run inside
available executor slots, never a queue. Health publication follows completed
scan/apply progress, including inside busy passes; a wedged loop cannot renew
admission evidence. Purge/reap stays solely on the legacy watchdog, which also
recovers async work under short leases when fleet ownership is stale. A local
RPC-progress monitor dumps stacks through the listener's nonblocking descriptor
helper and exits a wedged process; it never publishes health or changes the
listener's faulthandler timer. Handler filters sanitize primitive logs; allowlisted
Sentry money pages retain the router's alert classes. Deployment is separate from this code change.
"""
from __future__ import annotations

import datetime as dt
import faulthandler
import logging
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from trusted_router.config import Settings
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.services.settle_outbox_apply import ApplyOutcome
from trusted_router.services.settle_outbox_audit import row_ref as _row_ref
from trusted_router.stall_dump import stall_dump_fd
from trusted_router.storage_gcp_async_admission import (
    claim_health_publish,
    publish_health,
)
from trusted_router.storage_gcp_io import SpannerRpcCounter, count_spanner_rpcs
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox

logger = logging.getLogger(__name__)

def _audit_log(level: int, message: str, *args: Any) -> None:
    # Observability must never turn a successful money/lease RPC into a failure.
    try:
        logger.log(level, message, *args)
    except Exception:
        return  # Logging the logging failure would recurse.


class _ObservedOutbox(SpannerSettleOutbox):
    """Observe existing fenced return values without changing resolution SQL."""
    resolution: str | None = None
    ref: str = "none"

    def mark(self, *args: Any, **kwargs: Any) -> str | None:
        result = super().mark(*args, **kwargs)
        self.resolution = result
        _audit_log(logging.INFO, "async_drain.resolution status=%s fence_miss=%s ref=%s",
                   result, int(result is None), self.ref)
        return result

    def park(self, *args: Any, **kwargs: Any) -> bool:
        result = super().park(*args, **kwargs)
        self.resolution = "park" if result else None
        _audit_log(logging.INFO, "async_drain.resolution status=park parked=%s fence_miss=%s ref=%s",
                   int(result), int(not result), self.ref)
        return result


def _apply_slot(outbox: SpannerSettleOutbox, shard: int, lease: int,
                deadline: float, stop: threading.Event | None = None) -> tuple[int, str, int]:
    return _apply_slot_inner(outbox, shard, lease, deadline, stop)


def _apply_slot_inner(outbox: SpannerSettleOutbox, shard: int, lease: int,
                      deadline: float, stop: threading.Event | None) -> tuple[int, str, int]:
    # The slot has already started. There is no claimed executor queue tail.
    # Stop prevents new claims and new applies; already running applies finish.
    if (stop is not None and stop.is_set()) or time.monotonic() >= deadline:
        return 0, "", 0
    claim_started = time.monotonic()
    # Exact expansion of storage_gcp_settle_outbox.SpannerSettleOutbox.claim_shard.
    # claim_candidates mutates the winning candidate in place. This advisory
    # preimage is NOT a transactionally witnessed previous owner (races remain).
    candidates = outbox.due_shard(shard=shard)
    prior_leases = {id(candidate): (candidate.lease_owner, candidate.leased_until)
                    for candidate in candidates}
    rows = outbox.claim_candidates(candidates, lease_seconds=lease)
    claim_seconds = time.monotonic() - claim_started
    if not rows:
        return 0, "", 0
    row = rows[0]
    ref = _row_ref(row.payload_hash)
    prior_owner, prior_leased_until = prior_leases[id(row)]
    _audit_log(logging.INFO,
               "async_drain.claim ref=%s prior_owner=%s prior_leased_until=%s owner=%s leased_until=%s",
               ref, prior_owner or "none", prior_leased_until or "none",
               row.lease_owner or "none", row.leased_until or "none")
    # Claim RPCs can consume the lease/budget. Never start expired work, even
    # though the reservation fence would still prevent double billing.
    now = dt.datetime.now(dt.UTC)
    reason = None
    if stop is not None and stop.is_set():
        reason = "stop"
    elif time.monotonic() >= deadline:
        reason = "deadline"
    elif (row.leased_until is None
          or dt.datetime.fromisoformat(row.leased_until.replace('Z', '+00:00')) <= now):
        reason = "lease"
    if reason is not None:
        _audit_log(logging.INFO, "async_drain.deferred ref=%s owner=%s reason=%s",
                   ref, row.lease_owner or "none", reason)
        return 1, "deferred", 0
    started = time.monotonic()
    error_note = None
    apply_error = None
    try:
        outcome = drain.apply_frozen_settle(row)
    except Exception as exc:
        _audit_log(logging.WARNING, "async_drain.apply_failed error_class=%s", type(exc).__name__)
        outcome, apply_error = ApplyOutcome.ERROR, exc
        error_note = f"{type(exc).__name__}: {exc}"
    observed = _ObservedOutbox(outbox._database, outbox._pt, async_fence=outbox._async_fence)
    observed.ref = ref
    resolve_started = time.monotonic()
    try:
        drain._resolve_row(observed, row, outcome, error_note=error_note, apply_error=apply_error)
    except Exception as exc:
        _audit_log(logging.WARNING, "async_drain.resolve_failed error_class=%s", type(exc).__name__)
        outcome = "resolve_error"
    resolve_seconds = time.monotonic() - resolve_started
    # Pseudonymous reference only: never emit authorization/key/workspace IDs here.
    if observed.resolution == "done" and row.created_at is not None:
        try:
            created = dt.datetime.fromisoformat(row.created_at.replace('Z', '+00:00'))
            latency = (dt.datetime.now(dt.UTC) - created).total_seconds()
            _audit_log(logging.INFO, "async_drain.completed latency_seconds=%.6f ref=%s", latency, ref)
        except (ValueError, TypeError):
            _audit_log(logging.WARNING, "async_drain.completion_timestamp_invalid")
    elapsed = time.monotonic() - started
    _audit_log(logging.INFO,
               "async_drain.timing outcome=%s service_seconds=%.6f observed_at=%.6f "
               "ref=%s claim_seconds=%.6f resolve_seconds=%.6f",
               outcome, elapsed, time.time(), ref, claim_seconds, resolve_seconds)
    return 1, outcome, int(row.actual_cost_micro) if outcome == ApplyOutcome.SETTLED_NOW else 0


@dataclass
class WorkerCadence:
    """Process-local due times, with durable cadence still arbitrating overlaps.

    Only the drain thread calls these methods. HTTP liveness reads progress;
    it never refreshes progress or publishes health.
    """

    next_health: float = 0
    next_summary: float = 0
    progress_at: float = field(default_factory=time.monotonic)
    passes: int = 0
    claimed: int = 0
    _rpc_lock: Any = field(default_factory=threading.Lock)
    _rpc_counters: list[SpannerRpcCounter] = field(default_factory=list)
    _finished_rpcs: int = 0
    _observed_rpcs: int = 0

    @contextmanager
    def observe_io(self) -> Iterator[None]:
        # Observe the existing instrumentation only. No RPC deadline, retry,
        # priority or money primitive changes. RPC starts are local liveness,
        # never evidence for fleet health publication.
        with count_spanner_rpcs() as counter:
            with self._rpc_lock:
                self._rpc_counters.append(counter)
            try:
                yield
            finally:
                with self._rpc_lock:
                    self._finished_rpcs += counter.value()
                    self._rpc_counters.remove(counter)

    def stall_age(self) -> float:
        with self._rpc_lock:
            count = self._finished_rpcs + sum(c.value() for c in self._rpc_counters)
            if count != self._observed_rpcs:
                self._observed_rpcs = count
                self.progress_at = time.monotonic()
            return time.monotonic() - self.progress_at

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
        except Exception as exc:
            # Failed/slow publication ages out; never manufacture evidence.
            logger.warning("async_drain.health_publish_failed error_class=%s", type(exc).__name__)


def _observed_slot(cadence: WorkerCadence, *args: Any) -> tuple[int, str, int]:
    with cadence.observe_io():
        return _apply_slot(*args)


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
            futures = [pool.submit(_observed_slot, cadence, outbox, (shard + i) % 16, lease, deadline, stop)
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
    cadence.passes += 1
    cadence.claimed += claimed
    if claimed or outcomes:
        logger.info("async_drain.pass claimed=%s outcomes=%s elapsed_seconds=%.6f",
                    claimed, dict(outcomes), time.monotonic() - started)
    if time.monotonic() >= cadence.next_summary:
        logger.info("async_drain.summary passes=%s claimed=%s", cadence.passes, cadence.claimed)
        cadence.passes = cadence.claimed = 0
        cadence.next_summary = time.monotonic() + 60
    return dict(claimed=claimed, outcomes=dict(outcomes), recovered_micro=recovered,
                purged=0, reaped=0, deferred=outcomes.get("deferred", 0))


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
            with cadence.observe_io():
                drain_pass(limit, settings=settings, start_shard=shard, stop=stop, cadence=cadence)
        except Exception as exc:
            logger.warning("async_drain.pass_failed error_class=%s", type(exc).__name__)
            # An exception proves the loop is responsive, but does NOT publish.
            cadence.progress_at = time.monotonic()
        shard = (shard + 1) % 16
        stop.wait(max(0., settings.settle_outbox_poll_interval_seconds - (time.monotonic() - started)))


SHUTDOWN_JOIN_SECONDS = 8.0
MONITOR_INTERVAL_SECONDS = 0.1


def _exit_stalled_worker() -> None:
    """Immediate dump and exit; never change the listener's faulthandler timer."""
    try:
        # Code stacks only: no locals, exception payloads or row identifiers.
        faulthandler.dump_traceback(file=stall_dump_fd(sys.stderr.fileno()), all_threads=True)
    finally:
        os._exit(70)  # No shutdown/probe delay; durable leases/fences recover.


def monitor_worker(settings: Settings, stop: threading.Event, cadence: WorkerCadence) -> None:
    while not stop.wait(MONITOR_INTERVAL_SECONDS):
        if cadence.stall_age() < settings.settle_outbox_stall_seconds:
            continue
        _exit_stalled_worker()
        return  # reached only by test doubles


def create_worker_app(settings: Settings) -> FastAPI:
    """No auth routes, middleware, telemetry clients or unrelated starters."""
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)
    app.state.settings = settings
    stop = threading.Event()
    cadence = WorkerCadence()
    thread: threading.Thread | None = None
    monitor: threading.Thread | None = None
    cleanup_lock = threading.Lock()
    cleanups: list[Any] = []

    def cleanup() -> None:
        with cleanup_lock:
            while cleanups:
                cleanups.pop()()

    def loop() -> None:
        try:
            run_worker(settings, stop, cadence=cadence)
        finally:
            if stop.is_set():
                cleanup()


    @app.on_event("startup")
    async def _start_settle_drain() -> None:
        nonlocal thread, monitor
        from trusted_router.services.settle_drain_observability import (
            init_alerting,
            install_logging,
        )

        cleanups.append(install_logging())
        cleanups.append(init_alerting(settings))
        if not settings.settle_outbox_fast_drain_enabled:
            logger.info("async_drain.idle fast mode disabled")
            return
        cadence.progress_at = time.monotonic()
        thread = threading.Thread(target=loop, name="settle-drain-loop", daemon=True)
        thread.start()
        monitor = threading.Thread(target=monitor_worker, args=(settings, stop, cadence),
                                   name="settle-drain-monitor", daemon=True)
        monitor.start()

    @app.on_event("shutdown")
    async def _stop_settle_drain() -> None:
        stop.set()
        if monitor is not None:
            monitor.join(MONITOR_INTERVAL_SECONDS * 2)
        if thread is not None:
            # Bound the lifecycle wait below Cloud Run's SIGTERM grace. Active
            # money RPCs keep their existing deadlines/fences; SIGKILL may
            # abandon them and their rows become reclaimable at lease expiry.
            thread.join(SHUTDOWN_JOIN_SECONDS)
            if thread.is_alive():
                logger.warning("async_drain.shutdown_inflight")
        if thread is None or not thread.is_alive():
            cleanup()

    @app.get("/health")
    async def health() -> JSONResponse:
        idle = not settings.settle_outbox_fast_drain_enabled
        responsive = (thread is not None and thread.is_alive()
                      and cadence.stall_age() < settings.settle_outbox_stall_seconds)
        healthy = idle or responsive
        return JSONResponse({"status": "idle" if idle else "ok" if healthy else "stalled"},
                            status_code=200 if healthy else 503)

    return app
