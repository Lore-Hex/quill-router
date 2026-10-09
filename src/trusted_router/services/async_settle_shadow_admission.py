"""Timer-only admission observation. The money admission cache is never called."""
from __future__ import annotations

import asyncio
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from trusted_router.services.async_settle import TIER_CAPS, Admission, valid_health_record


def unknown(reason: str = "cache_missing") -> dict[str, Any]:
    return dict(prediction="unknown", reason=reason, tier=None, pending_micro=None,
                cap_micro=None, workspace_age_us=None, health_age_us=None, health_p95_us=None)


class Observer:
    def __init__(self, workspaces: frozenset[str], read: Callable[[str], Admission],
                 health_read: Callable[[], dict[str, Any] | None], *, cap: int = 0,
                 clock: Callable[[], float] = time.monotonic,
                 wall: Callable[[], float] = time.time,
                 enabled: Callable[[], bool] = lambda: True) -> None:
        self.enabled = enabled
        self.workspaces, self.read, self.health_read = workspaces, read, health_read
        self.cap, self.clock, self.wall = cap, clock, wall
        self.lock = threading.Lock()
        self.entries: dict[str, tuple[float, Admission]] = {}
        self.health: dict[str, Any] | None = None
        self.counts: Counter[str] = Counter()
        self.task: asyncio.Task[None] | None = None
        self.executor: ThreadPoolExecutor | None = None
        self.stopped = False
        self.budget_proven = False  # External publisher/scheduling/fleet gate.
        self.last_health_start: float | None = None
        self.last_workspace_start: dict[str, float] = {}
        self.progress_ok = True

    def install_workspace(self, key: str, started: float, value: Admission) -> None:
        if self.stopped or key not in self.workspaces:
            return
        with self.lock:
            previous = self.last_workspace_start.get(key)
            self.last_workspace_start[key] = started
            if self.clock() - started > .5 or previous is not None and started - previous > 4.25:
                self.progress_ok = False
            self.entries[key] = (started, value)

    def install_health(self, started: float, value: dict[str, Any] | None) -> None:
        if self.stopped:
            return
        with self.lock:
            if (self.clock() - started > .5 or self.last_health_start is not None
                    and started - self.last_health_start > 1.25):
                self.progress_ok = False
            self.last_health_start = started
            self.health = dict(value) if value is not None else None

    def peek(self, workspace: str) -> dict[str, Any]:
        if workspace not in self.workspaces or self.stopped:
            return unknown()
        if not self.lock.acquire(blocking=False):
            return unknown("cache_busy")
        try:
            result = unknown()
            item, health = self.entries.get(workspace), self.health
            if item is None or health is None:
                return result
            started, data = item
            if not valid_health_record(health) or not health["complete"]:
                return unknown("invalid_data")
            now, wall = self.clock(), self.wall()
            ages = (now - started, wall - health["observed_at"], wall - health["worker_heartbeat"])
            result.update(tier=data.tier, pending_micro=data.pending_micro,
                          workspace_age_us=max(0, int(ages[0] * 1e6)),
                          health_age_us=max(0, int(max(ages[1:]) * 1e6)),
                          health_p95_us=int(health["p95_age_seconds"] * 1e6))
            if not self.progress_ok or not all(0 <= age < 5 for age in ages):
                result["reason"] = "cache_stale"
                return result
            if type(data.pending_micro) is not int or data.pending_micro < 0 or type(data.tier) is not int:
                result["reason"] = "invalid_data"
                return result
            cap = self.cap or TIER_CAPS.get(data.tier)
            result["cap_micro"] = cap
            reason = ("ineligible_tier" if data.tier not in TIER_CAPS else
                      "cap_exceeded" if cap is not None and data.pending_micro > cap else
                      "drain_unhealthy" if health["p95_age_seconds"] > 5 else "eligible")
            result.update(prediction="yes" if reason == "eligible" else "no", reason=reason)
            return result
        finally:
            self.lock.release()

    def start(self) -> None:
        if self.workspaces and self.task is None:
            # Share two slots, prioritizing health starts over workspace starts.
            self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="settle-shadow-observe")
            self.task = asyncio.create_task(self.run())

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        origin = self.clock()
        next_health = origin
        ordered = sorted(self.workspaces)
        due = {key: origin + i * 4 / len(ordered) for i, key in enumerate(ordered)}
        health_job: asyncio.Future[Any] | None = None
        workspace_jobs: dict[str, asyncio.Future[Any]] = {}
        tokens, last = 2.0, origin
        while not self.stopped:
            if not self.enabled():
                self.stopped = True
                with self.lock:
                    self.entries.clear()
                    self.health = None
                return
            now = self.clock()
            tokens, last = min(2., tokens + max(0, now - last) * 10), now
            workspace_jobs = {key: job for key, job in workspace_jobs.items() if not job.done()}
            health_pending = health_job is not None and not health_job.done()
            active = len(workspace_jobs) + int(health_pending)
            if now >= next_health:
                if not health_pending and active < 2 and tokens >= 1:
                    tokens -= 1
                    if now - next_health > .25:
                        self.counts["missed_ticks"] += 1
                        self.progress_ok = False
                    next_health = now + 1  # Never enqueue catch-up reads.
                    self.counts["health_reads"] += 1
                    health_job = loop.run_in_executor(self.executor, self._health, now)
                    active += 1
                elif now - next_health > .25:
                    self.counts["missed_ticks"] += 1
                    self.progress_ok = False
                    next_health = now + 1
            # Due health has priority. Near its next start, reserve one token;
            # both slots otherwise serve workspaces to sustain 32 / 4 reads/s.
            key = min(due, key=lambda key: due[key]) if due else None
            needed = 2 if next_health - now < .1 else 1
            if key is not None and now >= due[key]:
                if (now < next_health and active < 2 and key not in workspace_jobs
                        and tokens >= needed):
                    tokens -= 1
                    if now - due[key] > .25:
                        self.counts["missed_ticks"] += 1
                        self.progress_ok = False
                    due[key] = now + 4
                    self.counts["workspace_reads"] += 1
                    workspace_jobs[key] = loop.run_in_executor(self.executor, self._workspace, key, now)
                elif now - due[key] > .25:
                    self.counts["missed_ticks"] += 1
                    self.progress_ok = False
                    due[key] = now + 4
            await asyncio.sleep(.01)

    def _workspace(self, key: str, started: float) -> None:
        try:
            self.install_workspace(key, started, self.read(key))
        except Exception:
            self.counts["read_failures"] += 1
            with self.lock:
                self.entries.pop(key, None)

    def _health(self, started: float) -> None:
        try:
            self.install_health(started, self.health_read())
        except Exception:
            self.counts["read_failures"] += 1
            with self.lock:
                self.health = None

    async def close(self) -> None:
        self.stopped = True
        if self.task is not None:
            await self.task
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            self.entries.clear()
            self.health = None
