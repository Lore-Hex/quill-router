"""Token-bucket rate limit counter for local/test, anonymous safe reads, and
internal gateway traffic (per-instance; see #399 for why the internal path
must not share a Spanner counter row).

Lives in its own module so storage.py doesn't carry the bucket-cleanup
loop. Shared production counters are in storage_gcp_rate_limits."""

from __future__ import annotations

import datetime as dt
import threading

from trusted_router.storage_models import RateLimitHit, utcnow

# Hard ceiling on live buckets. Production ingress subjects come from a trusted
# edge header and credential subjects are store-validated, but a cap remains a
# defense against configuration mistakes and legitimate high-cardinality load.
# At the cap, NEW subjects share one global overflow bucket: fabricated
# identities then throttle collectively instead of individually, and memory
# stays strictly bounded even if a future caller accidentally derives dynamic
# namespace names. 10k buckets is far above legitimate per-instance cardinality
# (a few hundred tenants + IPs per window) and small enough to be harmless.
_MAX_BUCKETS = 10_000
_OVERFLOW_WINDOW_SECONDS = 60


class InMemoryRateLimits:
    def __init__(self, *, lock: threading.RLock, max_buckets: int = _MAX_BUCKETS) -> None:
        self._lock = lock
        self._max_buckets = max(0, max_buckets)
        # Window length is part of the key. Otherwise two call sites that use
        # the same namespace with different configured windows can alias.
        self.buckets: dict[tuple[str, str, int, int], int] = {}
        self._last_cleanup_minute: int | None = None
        # Overflow state is fixed-size and deliberately outside ``buckets``.
        # Once the cardinality ceiling is reached, all new identities share a
        # conservative one-minute counter regardless of namespace.
        self._overflow_minute: int | None = None
        self._overflow_count = 0

    def reset(self) -> None:
        with self._lock:
            self.buckets.clear()
            self._last_cleanup_minute = None
            self._overflow_minute = None
            self._overflow_count = 0

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
        key = (namespace, subject, window_seconds, bucket)
        reset_epoch = (bucket + 1) * window_seconds
        with self._lock:
            # A full dictionary scan on every request makes the limiter itself
            # an O(n) CPU amplifier at its cardinality ceiling. One global
            # cleanup when the tumbling window advances provides the same
            # retention bound with amortized O(1) work on the hot path, and
            # avoids a second unbounded map of caller-provided namespaces.
            cleanup_minute = epoch // _OVERFLOW_WINDOW_SECONDS
            if self._last_cleanup_minute != cleanup_minute:
                stale = [
                    item
                    for item in self.buckets
                    if (item[3] + 3) * item[2] <= epoch
                ]
                for item in stale:
                    self.buckets.pop(item, None)
                self._last_cleanup_minute = cleanup_minute
            if key in self.buckets:
                count = self.buckets[key] + 1
                self.buckets[key] = count
            elif len(self.buckets) < self._max_buckets:
                count = 1
                self.buckets[key] = count
            else:
                # Cardinality cap reached: fold every new identity into one
                # fixed-size global counter instead of allocating another map
                # entry or another namespace-cleanup marker.
                if self._overflow_minute != cleanup_minute:
                    self._overflow_minute = cleanup_minute
                    self._overflow_count = 0
                self._overflow_count += 1
                count = self._overflow_count
                reset_epoch = (cleanup_minute + 1) * _OVERFLOW_WINDOW_SECONDS
        reset_at = dt.datetime.fromtimestamp(reset_epoch, dt.UTC).replace(microsecond=0)
        remaining = max(limit - count, 0)
        return RateLimitHit(
            allowed=count <= limit,
            limit=limit,
            remaining=remaining,
            reset_at=reset_at.isoformat().replace("+00:00", "Z"),
            retry_after_seconds=max(reset_epoch - epoch, 1),
        )
