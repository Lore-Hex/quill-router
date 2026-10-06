"""Postgres/Aurora-DSQL writer for the operational-analytics outbox.

Same four-method surface as
:class:`~trusted_router.storage_gcp_operational_analytics_outbox.SpannerOperationalAnalyticsOutbox`
so settle and probe code can hold either one without knowing which cloud it is
on.  The payload projection — and therefore the privacy contract — is shared
verbatim via :mod:`trusted_router.storage_operational_analytics`.

Durability differs from Spanner in one way that matters.  Spanner stamps each
row with ``PENDING_COMMIT_TIMESTAMP()``, which gives the drain a totally
ordered cursor.  Postgres has no such thing: ``now()`` is the transaction start
time, and a transaction that starts earlier can commit later, so a drain that
checkpointed on ``max(enqueued_at)`` would skip rows that materialise behind
its cursor.  So there is no cursor here.  The row itself is the queue entry,
keyed by ``(shard, event_kind, event_id)``, and the drain deletes what it has
successfully written.

That key does double duty as the idempotency guard.  ``_run_transaction``
retries Aurora DSQL's OCC aborts (SQLSTATE 40001), and a retried settle
re-runs this insert; ``ON CONFLICT DO NOTHING`` makes the replay a no-op
instead of a duplicate event.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from typing import Any

from trusted_router.storage_codec import json_body
from trusted_router.storage_models import Generation, SyntheticProbeSample
from trusted_router.storage_operational_analytics import (
    ACTIVITY_EVENT_KIND,
    CLIENT_EVENTS_EVENT_KIND,
    OPERATIONAL_ANALYTICS_OUTBOX_SHARDS,
    SYNTHETIC_EVENT_KIND,
    activity_payload,
    operational_analytics_shard,
    synthetic_payload,
)

OUTBOX_TABLE = "tr_operational_analytics_outbox"

# Written out rather than interpolated so the statement is obviously
# injection-free; every value is bound. ON CONFLICT DO NOTHING is what makes a
# retried transaction (Aurora DSQL aborts on OCC as SQLSTATE 40001) a no-op
# instead of a duplicate ClickHouse event.
INSERT_EVENT_SQL = (
    "INSERT INTO tr_operational_analytics_outbox "
    "(shard, event_kind, event_id, payload, enqueued_at) "
    "VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP) "
    "ON CONFLICT (shard, event_kind, event_id) DO NOTHING"
)

# The lag read, published in /status.json as `analytics.drain_lag_seconds`.
#
# THE definition of that statement, for both readers: the drain imports this
# name as its own `SELECT_OLDEST_SQL`
# (`clickhouse.ingest_operational_outbox_postgres`), so the number an outside
# observer reads and the number `backlog_alarm` fires on are the same query by
# construction rather than by two literals and a comment asking politely.
#
# One statement for the whole table, not one per shard: this runs on the
# request path behind the status cache, and 32 round trips per poll is what the
# drain's own metric used to cost before it was collapsed.
#
# Backed by tr_operational_analytics_outbox_enqueued_at_idx, so it is an index
# seek and its cost does not grow with the backlog -- which matters precisely
# because a growing backlog is when this number is most worth reading. There is
# deliberately no count(*) alongside it: that one IS a scan, and the lag already
# answers "is the drain alive?".
SELECT_OLDEST_ENQUEUED_AT_SQL = (
    "SELECT enqueued_at FROM tr_operational_analytics_outbox ORDER BY enqueued_at LIMIT 1"
)


class PostgresOperationalAnalyticsOutbox:
    """Append immutable operational events to a Postgres-wire outbox table."""

    def __init__(
        self,
        run_transaction: Callable[[Callable[[Any], None]], Any],
        *,
        shard_count: int = OPERATIONAL_ANALYTICS_OUTBOX_SHARDS,
    ) -> None:
        if shard_count < 1:
            raise ValueError("shard_count must be positive")
        self._run_transaction = run_transaction
        self._shard_count = shard_count

    def enqueue_activity(self, generation: Generation) -> None:
        self._enqueue(
            event_kind=ACTIVITY_EVENT_KIND,
            event_id=generation.id,
            payload=activity_payload(generation),
        )

    def enqueue_activity_tx(self, conn: Any, generation: Generation) -> None:
        """Enqueue activity on an existing connection inside its transaction.

        Gateway settlement uses this so the credit release, the bounded
        generation record, and the ClickHouse delivery intent commit together.
        ClickHouse itself is never in the inference transaction; the outbox row
        is the durable hand-off.
        """
        self._enqueue_tx(
            conn,
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

    def enqueue_synthetic_tx(self, conn: Any, sample: SyntheticProbeSample) -> None:
        self._enqueue_tx(
            conn,
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

    def oldest_enqueued_at_tx(self, conn: Any) -> dt.datetime | None:
        """``enqueued_at`` of the oldest undelivered row, or ``None`` if empty.

        ``None`` is fully drained, not unknown: rows are deleted only after
        every configured ClickHouse target has accepted them. Exceptions are
        NOT swallowed here -- the caller has to be able to tell "I looked and
        the queue is empty" from "I could not look", and a backend that
        returned ``None`` for both would publish an outage as perfect health.

        Takes a CONNECTION rather than running its own transaction, like every
        other ``*_tx`` method here. That is what lets the only caller --
        ``PostgresStore.operational_analytics_outbox_freshness`` -- put the read
        inside a bounded connection with a `statement_timeout`, which it must,
        because this one runs on the public /status.json path.
        """
        row = conn.execute(SELECT_OLDEST_ENQUEUED_AT_SQL).fetchone()
        if row is None or row[0] is None:
            return None
        value: dt.datetime = row[0]
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)

    def _enqueue(
        self,
        *,
        event_kind: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> None:
        def txn(conn: Any) -> None:
            self._enqueue_tx(
                conn,
                event_kind=event_kind,
                event_id=event_id,
                payload=payload,
            )

        self._run_transaction(txn)

    def _enqueue_tx(
        self,
        conn: Any,
        *,
        event_kind: str,
        event_id: str,
        payload: dict[str, Any],
    ) -> None:
        shard = operational_analytics_shard(
            f"{event_kind}:{event_id}",
            shard_count=self._shard_count,
        )
        conn.execute(
            INSERT_EVENT_SQL,
            (shard, event_kind, event_id, json_body(payload)),
        )
