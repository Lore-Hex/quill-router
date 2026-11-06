"""Optional read-ahead only: AdmissionCache retains its synchronous fallback."""
from __future__ import annotations

import threading
from contextvars import Context

from fastapi import FastAPI

from trusted_router.config import Settings
from trusted_router.services.async_settle import Admission, AdmissionCache, observe_health

ACTIVE_SECONDS = 30 * 60
HEALTH_SECONDS = 0.5
WORKSPACE_SECONDS = 3.0


class AdmissionRefresher:
    def __init__(self, cache: AdmissionCache) -> None:
        self.cache = cache
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=Context().run, args=(self._run,),
                                       name="async-settle-admission-refresh", daemon=True)
        self._pass_lock = threading.Lock()
        # Attempts include failures; evidence timestamps only describe successes.
        self._health_attempt = float("-inf")
        self._workspace_attempts: dict[str, float] = {}

    def refresh_once(self, now: float | None = None) -> None:
        """One pass, also drivable with a fake clock; never overlap passes.

        Production samples the cache clock immediately before each read. An
        explicit now fixes the clock for a deterministic, hand-driven pass.
        """
        if not self._pass_lock.acquire(blocking=False):
            return
        try:
            self._refresh(now)
        except Exception:  # noqa: S110 - optional read-ahead must not escape
            pass
        finally:
            self._pass_lock.release()

    def _refresh(self, now: float | None) -> None:
        cache = self.cache
        clock = cache.clock if now is None else lambda: now
        with cache.lock:
            started = clock()
            if self.stop_event.is_set() or not 0 <= started - cache.last_evaluated_at < ACTIVE_SECONDS:
                return
            # Tracking never outgrows the existing evidence cache.
            cache.evaluated_at = {ws: at for ws, at in cache.evaluated_at.items()
                                  if ws in cache.entries and 0 <= started - at < ACTIVE_SECONDS}
            workspaces = list(cache.evaluated_at)
            self._workspace_attempts = {ws: at for ws, at in self._workspace_attempts.items()
                                        if ws in cache.evaluated_at}
            health_due = (cache.health_read is not None
                          and started - self._health_attempt >= HEALTH_SECONDS
                          and (cache.health is None or started - cache.health_read_at >= HEALTH_SECONDS))
            if health_due:
                self._health_attempt = started
        if health_due and cache.health_read is not None:
            try:
                # A valid record showing an unhealthy drain is a successful read:
                # installing it closes cached-only admission at once, as a full-path
                # read of the same record does. A failed read, or an invalid,
                # incomplete or stale record, keeps the earlier evidence.
                health = observe_health(cache.health_read(), now=clock(), wall=cache.wall_clock())
                if health is not None:
                    with cache.lock:
                        if started > cache.health_read_at:
                            cache.health, cache.health_read_at = health, started
            except Exception:  # noqa: S110 - preserve old evidence on failed reads
                pass
        for workspace in workspaces:
            with cache.lock:
                started = clock()
                if self.stop_event.is_set() or not 0 <= started - cache.last_evaluated_at < ACTIVE_SECONDS:
                    return
                entry = cache.entries.get(workspace)
                active_at = cache.evaluated_at.get(workspace, float("-inf"))
                attempted = self._workspace_attempts.get(workspace, float("-inf"))
                if (entry is None or not 0 <= started - active_at < ACTIVE_SECONDS
                        or started - entry[0] < WORKSPACE_SECONDS
                        or started - attempted < WORKSPACE_SECONDS):
                    continue
                self._workspace_attempts[workspace] = started
            try:
                value = cache.read(workspace)
                # Tier eligibility/caps remain solely eligible()'s decision.
                if (not isinstance(value, Admission) or type(value.tier) is not int
                        or type(value.pending_micro) is not int or value.pending_micro < 0):
                    continue
                with cache.lock:
                    current = cache.entries.get(workspace)
                    if current is not None and started > current[0]:
                        cache.entries[workspace] = (started, value)
            except Exception:  # noqa: S110 - preserve old evidence on failed reads
                pass

    def _run(self) -> None:
        while not self.stop_event.wait(0.1):
            self.refresh_once()

    def close(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2.0)


def install(app: FastAPI, settings: Settings) -> None:
    """Register lifecycle hooks; constructing a cache/runtime starts no worker."""
    # Share the existing serving-only lifecycle seam; create_app stays pinned.
    if settings.async_settle_handoff_mode == "memory":
        from trusted_router.services.async_settle_memory import install as install_memory
        install_memory(app, settings)
    if not settings.async_settle_admission_refresh_enabled:
        return
    worker: AdmissionRefresher | None = None

    @app.on_event("startup")
    async def start() -> None:
        nonlocal worker
        try:
            if worker is not None or not settings.async_settle_admission_refresh_enabled:
                return
            runtime = getattr(app.state, "async_settle", None)
            cache = getattr(runtime, "admission", None)
            if not isinstance(cache, AdmissionCache) or cache.health_read is None:
                return
            candidate = AdmissionRefresher(cache)
            candidate.thread.start()
            worker = candidate
        except Exception:  # noqa: S110 - optional optimization cannot break startup
            pass

    @app.on_event("shutdown")
    async def stop() -> None:
        nonlocal worker
        if worker is not None:
            try:
                worker.close()
            except Exception:  # noqa: S110 - best-effort bounded shutdown
                pass
            finally:
                worker = None
