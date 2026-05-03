"""Spanner-backed rate limit counter using transactional read-modify-write."""

from __future__ import annotations

import datetime as dt
from typing import Any

from trusted_router.storage_gcp_io import SpannerIO
from trusted_router.storage_models import RateLimitHit, utcnow


class SpannerRateLimits:
    def __init__(self, io: SpannerIO) -> None:
        self._io = io

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
        bucket = int(now.timestamp()) // window_seconds
        entity_id = f"{namespace}#{subject}#{bucket}"
        reset_epoch = (bucket + 1) * window_seconds

        def txn(transaction: Any) -> int:
            row = self._io.read_entity_tx(transaction, "rate_limit", entity_id, dict)
            count = int(row.get("count", 0)) + 1 if row else 1
            self._io.write_entity_tx(
                transaction,
                "rate_limit",
                entity_id,
                {"count": count, "expires_at": reset_epoch},
            )
            return count

        count = self._io.database.run_in_transaction(txn)
        reset_at = dt.datetime.fromtimestamp(reset_epoch, dt.UTC).replace(microsecond=0)
        return RateLimitHit(
            allowed=count <= limit,
            limit=limit,
            remaining=max(limit - count, 0),
            reset_at=reset_at.isoformat().replace("+00:00", "Z"),
            retry_after_seconds=max(reset_epoch - int(now.timestamp()), 1),
        )
