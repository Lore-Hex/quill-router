"""Token-bucket rate limit counter for local/test, anonymous safe reads, and
internal gateway traffic (per-instance; see #399 for why the internal path
must not share a Spanner counter row).

Lives in its own module so storage.py doesn't carry the bucket-cleanup
loop. Shared production counters are in storage_gcp_rate_limits."""

from __future__ import annotations

import datetime as dt
import threading

from trusted_router.storage_models import RateLimitHit, utcnow

# Hard ceiling on live buckets. Subjects can be attacker-influenced (spoofed
# X-Forwarded-For identities, rotated credentials), so without a cap a request
# flood mints one dict entry per fabricated identity until the process OOMs.
# At the cap, NEW subjects share one per-namespace overflow bucket: fabricated
# identities then throttle collectively instead of individually, and memory
# stays bounded. 10k buckets is far above legitimate per-instance cardinality
# (a few hundred tenants + IPs per window) and small enough to be harmless.
_MAX_BUCKETS = 10_000
_OVERFLOW_SUBJECT = "__overflow__"


class InMemoryRateLimits:
    def __init__(self, *, lock: threading.RLock, max_buckets: int = _MAX_BUCKETS) -> None:
        self._lock = lock
        self._max_buckets = max_buckets
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
            if key not in self.buckets and len(self.buckets) >= self._max_buckets:
                # Cardinality cap reached: fold the new identity into the
                # namespace's shared overflow bucket rather than growing the map.
                key = (namespace, _OVERFLOW_SUBJECT, bucket)
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
