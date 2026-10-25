"""Process breaker and fleet latch; only the opted-in Service constructs this."""
from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Callable

from trusted_router.async_settle_shadow_journal import CONTROL_KIND, Row
from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore

CADENCE = 4.
WINDOW = 120.
THRESHOLD = 2
logger = logging.getLogger(__name__)


class RuntimeStop:
    def __init__(self, store: ObservationStore, actor: str, stop: Callable[[], None]) -> None:
        self.store, self.actor, self.stop = store, actor, stop
        self.lock = threading.Lock()
        self.failures: deque[float] = deque()
        self.tripped: Row | None = None
        self.persisted = False
        self.next_write = float('-inf')
        self.backoff = 1.
        self.next_read = float('-inf')
        self.task: asyncio.Task[None] | None = None
        store.database._shadow_v2_failure = self.outcome

    def outcome(self, method: str, outcome: str) -> None:
        """Called synchronously on IO workers; never performs IO itself."""
        if method not in {'commit', 'execute_streaming_sql'} or outcome not in {'DeadlineExceeded', 'Cancelled'}:
            return
        with self.lock:
            if self.tripped is not None:
                return
            now = self.store.clock()
            while self.failures and self.failures[0] <= now-WINDOW:
                self.failures.popleft()
            self.failures.append(now)
            if len(self.failures) < THRESHOLD:
                return
            stamp = int(self.store.wall()*1_000_000)
            self.tripped = Row.build(CONTROL_KIND, 'stop-v2', dict(v=2, policy_version='shadow-v2',
                actor=self.actor, stopped_at_us=stamp, reason='shadow RPC API-failure budget',
                incident=f'breaker:{self.actor}:{stamp}', source='breaker',
                failure_count=len(self.failures), window_seconds=int(WINDOW)))
            # Terminal latch is visible to workers/requests before this returns.
            self.stop()
            logger.warning("shadow_v2_runtime_breaker boot=%s failures=%s window_seconds=%s",
                           self.actor, THRESHOLD, int(WINDOW))

    def poll(self) -> bool:
        """One complete-key LOW read per cadence, including startup.

        An unavailable or malformed control is fail-closed locally. It cannot
        authorize shadow work, nor is it claimed as an observed fleet incident.
        """
        now = self.store.clock()
        if now < self.next_read:
            return True
        self.next_read = now+CADENCE
        try:
            if self.store.read_runtime_stop() is None:
                return True
        except Exception:  # noqa: S110 - unavailable control fails closed below
            pass
        self.stop()
        return False

    def retry(self) -> None:
        """Independent of shadow admission/close; retain the exact trip body."""
        row = self.tripped
        if row is None or self.persisted or self.store.clock() < self.next_write:
            return
        delay = self.backoff
        self.next_write = float("inf")
        self.backoff = min(60., delay*2)
        try:
            if not self.store.prepare_session():
                return
            self.store.runtime_stop(row, Budget(self.store.clock()+2, self.store.clock))
        except Exception:
            return
        finally:
            # A slow failed RPC must still be followed by the full backoff.
            self.next_write = self.store.clock()+delay
        self.persisted = True

    async def run(self, enabled: Callable[[], bool], alive: Callable[[], bool]) -> None:
        while alive() or (self.tripped is not None and not self.persisted):
            if self.tripped is not None:
                if not self.persisted and self.store.clock() >= self.next_write:
                    await asyncio.to_thread(self.retry)
            elif enabled() and self.store.clock() >= self.next_read:
                await asyncio.to_thread(self.poll)
            await asyncio.sleep(.05)

    def start(self, enabled: Callable[[], bool], alive: Callable[[], bool] | None = None) -> None:
        if self.task is None:
            self.task = asyncio.create_task(self.run(enabled, alive or enabled))

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
