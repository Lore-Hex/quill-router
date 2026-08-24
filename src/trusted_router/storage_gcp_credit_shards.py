"""Credit-ledger shard selection and allow-stale shard-count caching.

The billing cap remains hard because each shard owns a disjoint sub-budget.
This module only chooses the order in which those independent budgets are
tried; it never changes balances or decides whether a reserve is affordable.
"""

from __future__ import annotations

import random
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass

from trusted_router.storage_gcp_counters import credit_shard_count

DEFAULT_CACHE_TTL_SECONDS = 60.0
DEFAULT_CACHE_MAX_ENTRIES = 10_000
REFRESH_MIN_INTERVAL_SECONDS = 2.0
_LOAD_LOCK_STRIPES = 64


class CreditShardConfigurationMissingError(RuntimeError):
    """The typed balance exists without its authoritative shard configuration."""


def randomized_credit_shards(
    shard_count: int,
    *,
    rng: random.Random | None = None,
) -> tuple[int, ...]:
    """Return every configured shard exactly once in randomized order.

    The one-shard path deliberately avoids the RNG so its behavior is exactly
    the pre-sharding path. The returned tuple is built outside the Spanner
    callback and is therefore stable if Spanner retries an aborted transaction.
    """
    count = credit_shard_count({"shard_count": shard_count})
    if count == 1:
        return (0,)
    source = rng if rng is not None else random.SystemRandom()
    return tuple(source.sample(range(count), count))


@dataclass(frozen=True)
class _CacheEntry:
    shard_count: int
    expires_at: float
    loaded_at: float


class CreditShardCountCache:
    """Bounded LRU/TTL cache for a workspace's allow-stale shard count.

    A stale smaller value only reduces spreading and may reject early; a stale
    larger value scans retired/missing rows before a live row. Neither can
    create credit. Operator split/unshard still uses pause + drain + one atomic
    data transition, and explicitly invalidates this cache in its process.

    Striped miss locks prevent a burst for one workspace from stampeding the
    Spanner JSON row while allowing unrelated cache misses to load in parallel.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_CACHE_TTL_SECONDS,
        max_entries: int = DEFAULT_CACHE_MAX_ENTRIES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("credit shard cache TTL must be positive")
        if max_entries < 1:
            raise ValueError("credit shard cache max_entries must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._max_entries = int(max_entries)
        self._clock = clock
        self._entries: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._lock = threading.Lock()
        self._load_locks = tuple(threading.Lock() for _ in range(_LOAD_LOCK_STRIPES))

    def get(self, workspace_id: str, loader: Callable[[], int]) -> int:
        cached = self._get_fresh(workspace_id)
        if cached is not None:
            return cached

        load_lock = self._load_locks[hash(workspace_id) % len(self._load_locks)]
        with load_lock:
            cached = self._get_fresh(workspace_id)
            if cached is not None:
                return cached
            loaded = credit_shard_count({"shard_count": loader()})
            self._put(workspace_id, loaded)
            return loaded

    def invalidate(self, workspace_id: str) -> None:
        with self._lock:
            self._entries.pop(workspace_id, None)

    def refresh(self, workspace_id: str, loader: Callable[[], int]) -> int:
        """Force-reload the shard count, deduped under the stripe lock."""
        load_lock = self._load_locks[hash(workspace_id) % len(self._load_locks)]
        with load_lock:
            now = self._clock()
            with self._lock:
                entry = self._entries.get(workspace_id)
                if (
                    entry is not None
                    and now - entry.loaded_at < REFRESH_MIN_INTERVAL_SECONDS
                    # Never serve an expired entry (only possible when the TTL
                    # is configured below the refresh interval).
                    and now < entry.expires_at
                ):
                    self._entries.move_to_end(workspace_id)
                    return entry.shard_count
            loaded = credit_shard_count({"shard_count": loader()})
            self._put(workspace_id, loaded)
            return loaded

    def _get_fresh(self, workspace_id: str) -> int | None:
        now = self._clock()
        with self._lock:
            entry = self._entries.get(workspace_id)
            if entry is None:
                return None
            if entry.expires_at <= now:
                self._entries.pop(workspace_id, None)
                return None
            self._entries.move_to_end(workspace_id)
            return entry.shard_count

    def _put(self, workspace_id: str, shard_count: int) -> None:
        now = self._clock()
        entry = _CacheEntry(
            shard_count=shard_count,
            expires_at=now + self._ttl_seconds,
            loaded_at=now,
        )
        with self._lock:
            self._entries[workspace_id] = entry
            self._entries.move_to_end(workspace_id)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
