"""Best-effort enqueue side of the live analytics Spanner outbox.

The outbox is deliberately separate from gateway settlement. Analytics must
never make the money transaction slower or less reliable. A failed enqueue is
logged and tolerated by ``SpannerGenerations``; the Bigtable reconciler is the
completeness backstop.

The primary key starts with a deterministic shard. A commit timestamp alone is
a monotonically increasing key and would concentrate all writes on one Spanner
split. Within each shard, ``commit_ts`` is the live cursor: unlike benchmark
``created_at``, it records when Spanner committed the outbox row and therefore
cannot strand a late event behind an already-consumed range.
"""

from __future__ import annotations

import hashlib
from typing import Any

from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_models import ProviderBenchmarkSample

ANALYTICS_OUTBOX_SHARDS = 16


def analytics_outbox_shard(event_id: str, *, shard_count: int = ANALYTICS_OUTBOX_SHARDS) -> int:
    """Return a stable, evenly distributed shard for an analytics event."""
    if shard_count < 1:
        raise ValueError("shard_count must be positive")
    digest = hashlib.blake2b(event_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big") % shard_count


class SpannerAnalyticsOutbox:
    """Append immutable benchmark payloads using Spanner commit timestamps."""

    def __init__(
        self,
        database: Any,
        param_types: Any,
        *,
        shard_count: int = ANALYTICS_OUTBOX_SHARDS,
    ) -> None:
        if shard_count < 1:
            raise ValueError("shard_count must be positive")
        self._database = database
        self._pt = param_types
        self._shard_count = shard_count

    def enqueue(self, sample: ProviderBenchmarkSample) -> None:
        """Commit one immutable payload in its own transaction.

        Repeated calls intentionally create repeated outbox rows. Delivery is
        at-least-once and ClickHouse's ReplacingMergeTree collapses replays by
        the sample's stable ``id`` when queries use ``FINAL``.
        """
        shard = analytics_outbox_shard(sample.id, shard_count=self._shard_count)
        payload = json_body(sample)

        def txn(transaction: Any) -> None:
            transaction.execute_update(
                "INSERT INTO tr_analytics_outbox "
                "(shard, commit_ts, event_id, payload) "
                "VALUES (@shard, PENDING_COMMIT_TIMESTAMP(), @event_id, @payload)",
                params={
                    "shard": shard,
                    "event_id": sample.id,
                    "payload": payload,
                },
                param_types={
                    "shard": self._pt.INT64,
                    "event_id": self._pt.STRING,
                    "payload": self._pt.STRING,
                },
            )

        self._database.run_in_transaction(txn)
