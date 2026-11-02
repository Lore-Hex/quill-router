"""Best-effort shard configuration for zero-hold leased usage booking."""

from __future__ import annotations

import contextvars
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from trusted_router.storage_gcp_counters import key_usage_shard_count

CACHE_TTL_SECONDS = 120.0
CACHE_MAX_ENTRIES = 10_000
WAIT_SECONDS = 0.1
READ_DEADLINE_SECONDS = 0.5
RETRY_SECONDS = 10.0
# Bound even stalled session checkouts: no executor queue and no unbounded
# worker creation. Daemon readers never prevent process shutdown.
_READ_SLOTS = threading.BoundedSemaphore(8)
_READ_IDLE = threading.Condition()
_ACTIVE_READS = 0
CACHE_INIT_LOCK = threading.Lock()
log = logging.getLogger(__name__)


def _wait_for_idle_reads(timeout: float) -> bool:
    """Observe worker lifetime without inspecting semaphore internals."""
    with _READ_IDLE:
        return _READ_IDLE.wait_for(lambda: _ACTIVE_READS == 0, timeout=timeout)


def _release_read_slot(release: Callable[[], None]) -> None:
    global _ACTIVE_READS
    try:
        release()
    finally:
        with _READ_IDLE:
            _ACTIVE_READS -= 1
            if _ACTIVE_READS == 0:
                _READ_IDLE.notify_all()


def _run_read(task: Callable[[], None], release: Callable[[], None]) -> None:
    # Keep lifetime accounting outside patchable cache methods. Capture the
    # acquired slot's release method, so replacing _READ_SLOTS cannot misroute it.
    try:
        task()
    finally:
        _release_read_slot(release)


@dataclass
class _Entry:
    count: int | None = None
    next_attempt: float = 0.0
    wait_deadline: float = 0.0
    done: threading.Event = field(default_factory=threading.Event)
    logged: bool = False


class LeasedKeyUsageShardCache:
    """LRU cache serving the last good count during a background refresh.

    Only cold misses wait, for at most WAIT_SECONDS. Late fills remain useful.
    Evicted readers stay in _inflight (at most eight process-wide), allowing
    callers to share them; completion never re-inserts an evicted entry.
    Every entry mutation holds _lock; loaders run outside it in a fresh context.
    """

    def __init__(
        self, *, ttl_seconds: float = CACHE_TTL_SECONDS,
        max_entries: int = CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0 or max_entries < 1:
            raise ValueError("leased key cache TTL and size must be positive")
        self._ttl = ttl_seconds
        self._max_entries = max_entries
        self._clock = clock
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._inflight: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def get(self, key_hash: str, loader: Callable[[], int]) -> int:
        with self._lock:
            entry = self._entries.get(key_hash)
            if entry is None:
                entry = self._inflight.get(key_hash)
                if entry is None:
                    entry = _Entry()
                if len(self._entries) >= self._max_entries:
                    self._entries.popitem(last=False)
                self._entries[key_hash] = entry
            self._entries.move_to_end(key_hash)
            if key_hash not in self._inflight and self._clock() >= entry.next_attempt:
                self._start(key_hash, entry, loader)
            if entry.count is not None:
                return entry.count
            if key_hash not in self._inflight or entry.logged:
                return 1
            done = entry.done
            wait = max(0.0, entry.wait_deadline - time.monotonic())
        done.wait(wait)
        with self._lock:
            if entry.count is None and not entry.logged:
                self._failure(entry)
            return entry.count if entry.count is not None else 1

    def _failure(self, entry: _Entry) -> None:
        # Caller holds _lock. No key identifier, exception text or secret.
        entry.next_attempt = self._clock() + RETRY_SECONDS
        if not entry.logged:
            entry.logged = True
            if entry.count is None:
                log.warning("metric=leased_key_usage_shard_lookup_fallback_total value=1 fallback_shard=0")
            else:
                log.warning("metric=leased_key_usage_shard_refresh_failed_total value=1")

    def _start(self, key_hash: str, entry: _Entry, loader: Callable[[], int]) -> None:
        # Caller holds _lock. A separate event belongs to each attempt.
        global _ACTIVE_READS
        entry.logged = False
        slots = _READ_SLOTS
        if not slots.acquire(blocking=False):
            self._failure(entry)
            return
        with _READ_IDLE:
            _ACTIVE_READS += 1
        entry.done = threading.Event()
        now = time.monotonic()
        entry.wait_deadline = now + WAIT_SECONDS
        self._inflight[key_hash] = entry
        try:
            load = self._load
            threading.Thread(
                target=contextvars.Context().run,
                args=(_run_read, lambda: load(key_hash, entry, loader), slots.release),
                name="leased-key-usage", daemon=True,
            ).start()
        except Exception:
            del self._inflight[key_hash]
            _release_read_slot(slots.release)
            self._failure(entry)
            entry.done.set()

    def _load(self, key_hash: str, entry: _Entry, loader: Callable[[], int]) -> None:
        count: int | None = None
        try:
            count = key_usage_shard_count({"usage_shard_count": loader()})
        except Exception:
            count = None
        finally:
            # Retire the attempt even on BaseException, which still propagates.
            # The loader owns its RPC deadline; every validated result is safe
            # to cache regardless of scheduling or session-checkout delay.
            with self._lock:
                if count is None:
                    self._failure(entry)
                else:
                    entry.count = count
                    entry.next_attempt = self._clock() + self._ttl
                del self._inflight[key_hash]
                entry.done.set()
