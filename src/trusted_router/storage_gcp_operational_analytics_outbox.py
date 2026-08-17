"""Durable outbox for tenant activity and synthetic status metadata.

This outbox is separate from the public/provider benchmark stream because its
tables have different privacy and retention boundaries.  Raw workspace and API
key identifiers never leave Spanner: ClickHouse receives stable one-way
surrogates and content-free request metadata only.

The constants and the payload projection are backend-neutral and now live in
:mod:`trusted_router.storage_operational_analytics` so the Postgres/DSQL
adapter can share them without importing from a ``storage_gcp_*`` module.
They are re-exported here so every existing import site keeps working
unchanged.  What stays is the Spanner-specific writer.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from trusted_router.storage_gcp_codec import json_body
from trusted_router.storage_models import Generation, SyntheticProbeSample
from trusted_router.storage_operational_analytics import (
    ACTIVITY_EVENT_KIND,
    CLIENT_EVENTS_EVENT_KIND,
    OPERATIONAL_ANALYTICS_OUTBOX_SHARDS,
    SYNTHETIC_EVENT_KIND,
    activity_payload,
    analytics_surrogate,
    operational_analytics_shard,
    synthetic_payload,
)

__all__ = [
    "ACTIVITY_EVENT_KIND",
    "CLIENT_EVENTS_EVENT_KIND",
    "OPERATIONAL_ANALYTICS_OUTBOX_SHARDS",
    "SYNTHETIC_EVENT_KIND",
    "SpannerOperationalAnalyticsOutbox",
    "activity_payload",
    "analytics_surrogate",
    "operational_analytics_shard",
    "synthetic_payload",
]


class SpannerOperationalAnalyticsOutbox:
    """Append immutable operational events using Spanner commit timestamps."""

    def __init__(
        self,
        database: Any,
        param_types: Any,
        *,
        shard_count: int = OPERATIONAL_ANALYTICS_OUTBOX_SHARDS,
    ) -> None:
        if shard_count < 1:
            raise ValueError("shard_count must be positive")
        self._database = database
        self._pt = param_types
        self._shard_count = shard_count

    def enqueue_activity(self, generation: Generation) -> None:
        self._enqueue(
            event_kind=ACTIVITY_EVENT_KIND,
            event_id=generation.id,
            payload=activity_payload(generation),
        )

    def enqueue_activity_tx(self, transaction: Any, generation: Generation) -> None:
        """Enqueue activity in an existing Spanner transaction.

        Gateway settlement uses this method so the charge, bounded generation
        record, and ClickHouse delivery intent commit atomically.  The outbox
        row remains the durable hand-off; ClickHouse itself is never in the
        inference transaction.
        """
        self._enqueue_tx(
            transaction,
            event_kind=ACTIVITY_EVENT_KIND,
            event_id=generation.id,
            payload=activity_payload(generation),
        )

    def enqueue_synthetic(self, sample: SyntheticProbeSample) -> None:
        self._enqueue(
            event_kind=SYNTHETIC_EVENT_KIND,
            event_id=sample.id,
            payload=synthetic_payload(sample),
        )

    def enqueue_synthetic_tx(
        self,
        transaction: Any,
        sample: SyntheticProbeSample,
    ) -> None:
        self._enqueue_tx(
            transaction,
            event_kind=SYNTHETIC_EVENT_KIND,
            event_id=sample.id,
            payload=synthetic_payload(sample),
        )

    def enqueue_client_events(self, payload: dict[str, Any]) -> None:
        self._enqueue(
            event_kind=CLIENT_EVENTS_EVENT_KIND,
            event_id=f"{payload['tenant_id']}:{payload['batch_id']}",
            payload=payload,
        )

    def oldest_enqueued_at(self) -> dt.datetime | None:
        """Commit timestamp of the oldest undelivered row, or ``None`` if empty.

        Spanner's column is ``commit_ts``, not ``enqueued_at`` -- the method is
        named for the contract it feeds (``analytics.oldest_enqueued_at`` in
        /status.json) rather than for one backend's column, so the publisher
        can hold either outbox without knowing which cloud it is on.

        Per shard rather than one global ``ORDER BY commit_ts LIMIT 1``: the
        table's primary key leads with ``shard``, so the global form is a scan
        of the whole outbox and gets more expensive exactly as the backlog it
        is measuring grows. Each shard read is a seek on the key prefix, and
        the oldest of the 32 heads is the oldest row in the table.

        This is the same read the Spanner drain performs
        (``clickhouse.ingest_operational_outbox.oldest_commit_ts``); keeping
        them identical is what stops the published number and the drain's own
        ``backlog_alarm`` from meaning different things.
        """
        oldest: dt.datetime | None = None
        with self._database.snapshot(multi_use=True) as snapshot:
            for shard in range(self._shard_count):
                rows = list(
                    snapshot.execute_sql(
                        "SELECT commit_ts FROM tr_operational_analytics_outbox "
                        "WHERE shard=@shard ORDER BY commit_ts LIMIT 1",
                        params={"shard": shard},
                        param_types={"shard": self._pt.INT64},
                    )
                )
                if not rows or rows[0][0] is None:
                    continue
                candidate: dt.datetime = rows[0][0]
                if candidate.tzinfo is None:
                    candidate = candidate.replace(tzinfo=dt.UTC)
                oldest = candidate if oldest is None else min(oldest, candidate)
        return oldest

    def _enqueue(
        self,
        *,
        event_kind: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> None:
        def txn(transaction: Any) -> None:
            self._enqueue_tx(
                transaction,
                event_kind=event_kind,
                event_id=event_id,
                payload=payload,
            )

        self._database.run_in_transaction(txn)

    def _enqueue_tx(
        self,
        transaction: Any,
        *,
        event_kind: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> None:
        shard = operational_analytics_shard(
            f"{event_kind}:{event_id}",
            shard_count=self._shard_count,
        )
        transaction.execute_update(
            "INSERT INTO tr_operational_analytics_outbox "
            "(shard, commit_ts, event_kind, event_id, payload) "
            "VALUES (@shard, PENDING_COMMIT_TIMESTAMP(), @event_kind, "
            "@event_id, @payload)",
            params={
                "shard": shard,
                "event_kind": event_kind,
                "event_id": event_id,
                "payload": json_body(payload),
            },
            param_types={
                "shard": self._pt.INT64,
                "event_kind": self._pt.STRING,
                "event_id": self._pt.STRING,
                "payload": self._pt.STRING,
            },
        )
