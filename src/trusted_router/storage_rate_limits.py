"""Token-bucket rate limit counter for the in-memory store.

Lives in its own module so storage.py doesn't carry the bucket-cleanup
loop. Production Spanner version is in storage_gcp_rate_limits."""

from __future__ import annotations

import datetime as dt
import threading

from trusted_router.storage_models import RateLimitHit, utcnow


class InMemoryRateLimits:
    def __init__(self, *, lock: threading.RLock) -> None:
        self._lock = lock
        self.buckets: dict[tuple[str, str, int], int] = {}

    def reset(self) -> None:
        self.buckets.clear()

    def hit(
        self,
        *,
        namespace: str,
        subject: str,
        limit: int,
        window_seconds: int,
        now: dt.datetime | None = None,
    ) -> RateLimitHit:
        now = now or utcnow()
        epoch = int(now.timestamp())
        bucket = epoch // window_seconds
        key = (namespace, subject, bucket)
        reset_epoch = (bucket + 1) * window_seconds
        reset_at = dt.datetime.fromtimestamp(reset_epoch, dt.UTC).replace(microsecond=0)
        with self._lock:
            count = self.buckets.get(key, 0) + 1
            self.buckets[key] = count
            # Opportunistic cleanup keeps local/test memory bounded.
            stale = [
                item
                for item in self.buckets
                if item[0] == namespace and item[2] < bucket - 2
            ]
            for item in stale:
                self.buckets.pop(item, None)
        remaining = max(limit - count, 0)
        return RateLimitHit(
            allowed=count <= limit,
            limit=limit,
            remaining=remaining,
            reset_at=reset_at.isoformat().replace("+00:00", "Z"),
            retry_after_seconds=max(reset_epoch - epoch, 1),
        )
