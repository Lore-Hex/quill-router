"""Best-effort enqueue side of the live analytics Spanner outbox.

Analytics must never make money less reliable. A standalone enqueue is its own
transaction; a failure is logged and tolerated by ``SpannerGenerations`` and
the durable-delivery repair is the completeness backstop. The one exception is
the one-commit settle (``typed_finalize_atomic(settle_outbox_intent=...)``),
where this append-only, read-free INSERT rides in the money commit. That
transaction has a complete fallback (the durable two-commit settle, which
records the benchmark after commit exactly as before), so an analytics failure
there costs the fallback's extra commits, never the charge.

The primary key starts with a deterministic shard. A commit timestamp alone is
a monotonically increasing key and would concentrate all writes on one Spanner
split. Within each shard, ``commit_ts`` is the live cursor: unlike benchmark
``created_at``, it records when Spanner committed the outbox row and therefore
cannot strand a late event behind an already-consumed range.
"""

from __future__ import annotations

import hashlib
from typing import Any

from trusted_router.storage_gcp_batch_dml import DmlStatement
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
        def txn(transaction: Any) -> None:
            self.enqueue_tx(transaction, sample)

        self._database.run_in_transaction(txn)

    def enqueue_tx(self, transaction: Any, sample: ProviderBenchmarkSample) -> None:
        """Enqueue a benchmark in an existing Spanner transaction."""
        sql, params, types = self.enqueue_statement(sample)
        transaction.execute_update(sql, params=params, param_types=types)

    def enqueue_statement(self, sample: ProviderBenchmarkSample) -> DmlStatement:
        """The benchmark INSERT, for one batch with other DML.

        PENDING_COMMIT_TIMESTAMP() makes this the last touch of
        tr_analytics_outbox in its transaction; nothing else there reads or
        writes that table.
        """
        shard = analytics_outbox_shard(sample.id, shard_count=self._shard_count)
        return (
            "INSERT INTO tr_analytics_outbox "
            "(shard, commit_ts, event_id, payload) "
            "VALUES (@shard, PENDING_COMMIT_TIMESTAMP(), @event_id, @payload)",
            {
                "shard": shard,
                "event_id": sample.id,
                "payload": json_body(sample),
            },
            {
                "shard": self._pt.INT64,
                "event_id": self._pt.STRING,
                "payload": self._pt.STRING,
            },
        )
