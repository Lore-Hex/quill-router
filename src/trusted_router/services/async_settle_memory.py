"""Bounded, deliberately volatile async-v1 handoffs (Joseph, 2026-11-03).

One FIFO writer per serving process. Neither the index nor its spend accounting
is durable. An uncertain commit retries exactly the frozen identity and payload.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from contextvars import Context
from dataclasses import dataclass, replace

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from trusted_router.config import Settings
from trusted_router.services import async_settle_handler as handler
from trusted_router.services.async_settle import Runtime
from trusted_router.services.settle_outbox_drain import spanner_settle_outbox
from trusted_router.storage_gcp_io import spanner_rpc_deadline
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
from trusted_router.storage_models import SettleOutboxRow

logger = logging.getLogger("trusted_router.async_settle_timing")
ITEM_CAP = 64
AMOUNT_CAP_MICRO = 1_000_000
MAX_AGE_SECONDS = 10.0
ATTEMPT_SECONDS = 5.0
RETRY_SECONDS = 120.0
# This bounds only the writer's close, not SIGTERM-to-flush: serve.py waits for
# in-flight requests before lifespan cleanup. Rows not flushed before process
# termination are an accepted loss, even if the close hook never runs.
SHUTDOWN_SECONDS = 5.0


@dataclass
class Pending:
    row: SettleOutboxRow
    accepted_at: float
    next_at: float
    attempts: int = 0
    queue_wait_ms: float = 0
    enqueue_ms: float = 0


class MemoryHandoff:
    def __init__(self, *, outbox: Callable[[], SpannerSettleOutbox] = spanner_settle_outbox,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.outbox, self.clock = outbox, clock
        self.condition = threading.Condition()
        # Insertion order is the queue; the in-flight head stays indexed/capped.
        self.index: dict[tuple[str, str], Pending] = {}
        self.accepting = True
        self.stopping_at = float("inf")
        self.lost = 0
        self.thread = threading.Thread(target=Context().run, args=(self._run,),
                                       name="async-settle-memory-writer", daemon=True)

    def get(self, authorization_id: str, intent_kind: str) -> SettleOutboxRow | None:
        with self.condition:
            entry = self.index.get((authorization_id, intent_kind))
            return replace(entry.row) if entry is not None else None

    def pending(self, workspace_id: str) -> int:
        with self.condition:
            return sum(p.row.actual_cost_micro for p in self.index.values()
                       if p.row.workspace_id == workspace_id)

    def accept(self, row: SettleOutboxRow) -> JSONResponse:
        with self.condition:
            key = (row.authorization_id, row.intent_kind)
            existing = self.index.get(key)
            duplicate = handler.duplicate_response(existing.row if existing else None, row, resolve=False)
            if duplicate is not None:
                return duplicate
            now = self.clock()
            oldest = next(iter(self.index.values()), None)
            if (not self.accepting or not self.thread.is_alive()
                    or (oldest is not None and now - oldest.accepted_at > MAX_AGE_SECONDS)):
                return handler.sync_required("handoff_unhealthy")
            if (len(self.index) >= ITEM_CAP
                    or sum(p.row.actual_cost_micro for p in self.index.values())
                    + row.actual_cost_micro > AMOUNT_CAP_MICRO):
                return handler.sync_required("handoff_queue_full")
            self.index[key] = Pending(replace(row), now, now)
            self.condition.notify_all()
            handler.metric("accepted")
            return handler.response(row)

    def _drop(self, key: tuple[str, str], entry: Pending, outcome: str) -> None:
        # Caller holds condition. Shutdown may already have accounted for this
        # entry while its RPC was still in flight; never double-count it.
        if self.index.get(key) is not entry:
            return
        del self.index[key]
        if entry.attempts == 0:
            entry.queue_wait_ms = max(0, self.clock() - entry.accepted_at) * 1000
        if outcome == "lost":
            self.lost += 1
        logger.info("metric=async_settle_memory_commit queue_wait_ms=%.3f enqueue_ms=%.3f "
                    "attempts=%d outcome=%s count=1", entry.queue_wait_ms, entry.enqueue_ms,
                    entry.attempts, outcome)
        self.condition.notify_all()

    def write_once(self) -> bool:
        """Single writer only; exposed for deterministic fake-clock tests."""
        with self.condition:
            if not self.index:
                return False
            key, entry = next(iter(self.index.items()))
            now = self.clock()
            budget = min(entry.accepted_at + RETRY_SECONDS, self.stopping_at)
            if now >= budget:
                self._drop(key, entry, "lost")
                return True
            if now < entry.next_at:
                return False
            if entry.attempts == 0:
                entry.queue_wait_ms = max(0, now - entry.accepted_at) * 1000
            entry.attempts += 1
            deadline = min(now + ATTEMPT_SECONDS, budget)
        started = self.clock()
        try:
            with spanner_rpc_deadline(deadline):
                outcome, _ = handler.enqueue_outcome(self.outbox(), replace(entry.row), deadline)
        except Exception:
            # Includes unknown commit results and duplicate reconciliation reads.
            # No exception text or request identifiers enter logs.
            outcome = "retry"
        with self.condition:
            entry.enqueue_ms += max(0, self.clock() - started) * 1000
            if outcome not in {"retry", "unknown"}:
                self._drop(key, entry, outcome)
            elif self.clock() >= budget:
                self._drop(key, entry, "lost")
            else:
                entry.next_at = self.clock() + min(2.0, 0.1 * 2 ** min(entry.attempts - 1, 5))
        return True

    def _run(self) -> None:
        while True:
            with self.condition:
                if not self.accepting and not self.index:
                    return
            if not self.write_once():
                with self.condition:
                    self.condition.wait(timeout=0.05)

    def close(self) -> None:
        with self.condition:
            self.accepting = False
            self.stopping_at = self.clock() + SHUTDOWN_SECONDS
            self.condition.notify_all()
        if self.thread.ident is not None:
            self.thread.join(timeout=SHUTDOWN_SECONDS)
        with self.condition:
            for key, entry in list(self.index.items()):
                self._drop(key, entry, "lost")


def install(app: FastAPI, settings: Settings) -> None:
    if (settings.async_settle_handoff_mode != "memory"
            or not settings.async_settle_admission_enabled
            or settings.service_surface not in {"combined", "internal"}):
        return
    worker: MemoryHandoff | None = None

    @app.on_event("startup")
    async def start() -> None:
        nonlocal worker
        runtime = getattr(app.state, "async_settle", None)
        if worker is not None or not isinstance(runtime, Runtime) or runtime.admission is None:
            return
        worker = MemoryHandoff()
        runtime.memory = worker
        worker.thread.start()

    @app.on_event("shutdown")
    async def stop() -> None:
        nonlocal worker
        if worker is not None:
            try:
                worker.close()
            finally:
                worker = None
