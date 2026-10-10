"""Timer-only admission observation. The money admission cache is never called."""
from __future__ import annotations

import asyncio
import datetime as dt
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
        self.failures: Counter[str | None] = Counter()
        self.failed_ticks: dict[str | None, float] = {}  # Recovery ordering only.
        self.failed_tick_ids: dict[str | None, set[float]] = {}
        self.inflight_ticks: dict[str | None, float] = {}
        self.stale: set[str | None] = set()
        self.degraded_since: float | None = None
        self.degraded_seconds = 0.
        self.interval_max_consecutive_failures = 0
        # Runtime supplies the counter wall clock before starting the observer.
        self.evidence_wall = wall
        self.daily_since = clock()
        self.daily_counts: dict[str, dict[str, int | float]] = {}
        self.daily_overflow = False
        self.daily_sealed_before = ""

    @property
    def progress_ok(self) -> bool:
        return not any(count >= (3 if key is None else 2)
                       for key, count in self.failures.items())

    def _daily_bucket(self, wall: float) -> dict[str, int | float]:
        day = dt.datetime.fromtimestamp(wall, dt.UTC).date().isoformat()
        if day < self.daily_sealed_before:
            # A wall-clock reversal cannot reopen an already sealed day.
            self.daily_overflow = True
            return dict(max_consecutive_failures=0, degraded_seconds=0., started_at_us=int(wall * 1e6))
        bucket = self.daily_counts.setdefault(day, dict(
            max_consecutive_failures=0, degraded_seconds=0., started_at_us=int(wall * 1e6)))
        bucket["started_at_us"] = min(bucket["started_at_us"], int(wall * 1e6))
        if len(self.daily_counts) > 3:
            del self.daily_counts[min(self.daily_counts)]
            self.daily_overflow = True
        return bucket

    def _account_daily(self, now: float) -> None:
        # Split monotonic elapsed time at UTC boundaries. Keep fractions until
        # Runtime takes the ceiling of each day's cumulative duration.
        wall = self.evidence_wall()
        left = self.daily_since
        streak = max(self.failures.values(), default=0)
        while left < now:
            left_wall = wall - (now - left)
            midnight = (left_wall // 86400 + 1) * 86400
            right = min(now, now - (wall - midnight))
            bucket = self._daily_bucket(left_wall)
            bucket["max_consecutive_failures"] = max(bucket["max_consecutive_failures"], streak)
            if self.degraded_since is not None:
                bucket["degraded_seconds"] += right - left
            left = right
        bucket = self._daily_bucket(wall)
        bucket["max_consecutive_failures"] = max(bucket["max_consecutive_failures"], streak)
        self.daily_since = now
        self.daily_sealed_before = max(self.daily_sealed_before,
            dt.datetime.fromtimestamp(wall, dt.UTC).date().isoformat())

    def _count(self, field: str) -> None:
        self.counts[field] += 1
        bucket = self._daily_bucket(self.evidence_wall())
        bucket[field] = bucket.get(field, 0) + 1

    def snapshot_daily_counts(self) -> tuple[dict[str, dict[str, int | float]], bool, str]:
        with self.lock:
            self._account_degradation(self.clock())
            # Cumulative copies survive failed/partial folds. Only a sealed,
            # successfully folded day can be removed; callbacks keep adding
            # to the open day without changing this snapshot's boundary.
            return ({day: dict(values) for day, values in self.daily_counts.items()},
                    self.daily_overflow, self.daily_sealed_before)

    def acknowledge_daily_counts(self, day: str, sealed_before: str) -> None:
        with self.lock:
            if day < sealed_before:
                self.daily_counts.pop(day, None)

    def _account_degradation(self, now: float) -> None:
        self._account_daily(now)
        # Union of degraded reader intervals, independent of request/peek rate.
        if self.degraded_since is not None:
            self.degraded_seconds += max(0., now - self.degraded_since)
        self.degraded_since = now if not self.stopped and not self.progress_ok else None

    def _failure(self, key: str | None, tick: float, field: str) -> None:
        # Caller holds lock. Count each cause, but only one failed tick per reader.
        if self.stopped:
            return
        self._account_degradation(self.clock())
        self._count(field)
        identities = self.failed_tick_ids.setdefault(key, set())
        if tick not in identities:
            self.failures[key] += 1
            identities.add(tick)
        self.failed_ticks[key] = max(tick, self.failed_ticks.get(key, float("-inf")))
        # At most one executor job per reader can complete out of order. Keep
        # its identity independently of the newest skipped/late-start tick.
        identities.intersection_update({self.failed_ticks[key], self.inflight_ticks.get(key)})
        self.stale.add(key)
        self.counts["max_consecutive_failures"] = max(
            self.counts["max_consecutive_failures"], self.failures[key])
        self.interval_max_consecutive_failures = max(
            self.interval_max_consecutive_failures, self.failures[key])
        self._account_degradation(self.clock())

    def _success(self, key: str | None, started: float) -> bool:
        # A late-start tick or an older in-flight result cannot undo a failure.
        if started <= self.failed_ticks.get(key, float("-inf")):
            return False
        self._account_degradation(self.clock())
        self.failures[key] = 0
        self.failed_tick_ids.pop(key, None)
        self.stale.discard(key)
        self._account_degradation(self.clock())
        return True

    def snapshot_counts(self) -> dict[str, int | float]:
        with self.lock:
            self._account_degradation(self.clock())
            result: dict[str, int | float] = dict(self.counts)
            result["degraded_seconds"] = self.degraded_seconds
            result["max_consecutive_failures"] = self.interval_max_consecutive_failures
            # Runtime folds maxima into its daily counter, never subtracts them.
            self.interval_max_consecutive_failures = max(self.failures.values(), default=0)
            return result

    def missed_tick(self, key: str | None, tick: float) -> None:
        with self.lock:
            self._failure(key, tick, "missed_ticks")

    def install_workspace(self, key: str, started: float, value: Admission) -> None:
        if self.stopped or key not in self.workspaces:
            return
        with self.lock:
            if self.clock() - started > .5:
                self._failure(key, started, "late_installs")
            elif self._success(key, started):
                self.entries[key] = (started, value)

    def install_health(self, started: float, value: dict[str, Any] | None) -> None:
        if self.stopped:
            return
        with self.lock:
            if self.clock() - started > .5:
                self._failure(None, started, "late_installs")
            elif self._success(None, started):
                self.health = dict(value) if value is not None else None

    def peek(self, workspace: str) -> dict[str, Any]:
        if workspace not in self.workspaces or self.stopped:
            return unknown()
        if not self.lock.acquire(blocking=False):
            return unknown("cache_busy")
        try:
            stale = workspace in self.stale or None in self.stale
            result = unknown("cache_stale" if stale else "cache_missing")
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
            if stale or not all(0 <= age < 5 for age in ages):
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
                    self._account_degradation(self.clock())
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
                        self.missed_tick(None, now)
                    next_health = now + 1  # Never enqueue catch-up reads.
                    with self.lock:
                        self._count("health_reads")
                        self.inflight_ticks[None] = now
                    health_job = loop.run_in_executor(self.executor, self._health, now)
                    active += 1
                elif now - next_health > .25:
                    self.missed_tick(None, now)
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
                        self.missed_tick(key, now)
                    due[key] = now + 4
                    with self.lock:
                        self._count("workspace_reads")
                        self.inflight_ticks[key] = now
                    workspace_jobs[key] = loop.run_in_executor(self.executor, self._workspace, key, now)
                elif now - due[key] > .25:
                    self.missed_tick(key, now)
                    due[key] = now + 4
            await asyncio.sleep(.01)

    def _workspace(self, key: str, started: float) -> None:
        try:
            self.install_workspace(key, started, self.read(key))
        except Exception:
            with self.lock:
                self._failure(key, started, "read_failures")

    def _health(self, started: float) -> None:
        try:
            self.install_health(started, self.health_read())
        except Exception:
            with self.lock:
                self._failure(None, started, "read_failures")

    async def close(self) -> None:
        self.stopped = True
        if self.task is not None:
            await self.task
        if self.executor is not None:
            self.executor.shutdown(wait=False, cancel_futures=True)
        with self.lock:
            self._account_degradation(self.clock())
            self.degraded_since = None
            self.entries.clear()
            self.health = None
