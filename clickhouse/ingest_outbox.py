"""Continuously drain the Spanner analytics outbox into local ClickHouse.

The durable cursor is the outbox itself: ClickHouse must acknowledge a batch
before its exact Spanner primary keys are deleted. A crash between those two
operations replays the batch, which is safe because canonical queries use
``FINAL`` over a ReplacingMergeTree.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Protocol

from clickhouse.backfill_benchmark_samples import normalise

PROJECT = "quill-cloud-proxy"
SPANNER_INSTANCE = "trusted-router-nam6"
SPANNER_DATABASE = "trusted-router"
OUTBOX_TABLE = "tr_analytics_outbox"
CLICKHOUSE_DATABASE = "tr"
CLICKHOUSE_TABLE = "provider_benchmark_samples"
OUTBOX_SHARDS = 16

log = logging.getLogger("trusted_router.analytics_ingest")


@dataclass(frozen=True)
class OutboxRow:
    shard: int
    commit_ts: dt.datetime
    event_id: str
    payload: str

    @property
    def key(self) -> tuple[int, dt.datetime, str]:
        return (self.shard, self.commit_ts, self.event_id)


@dataclass
class DrainMetrics:
    rows_ingested_total: int = 0
    clickhouse_insert_errors_total: int = 0


@dataclass(frozen=True)
class DrainResult:
    fetched: int
    inserted: int
    rows_per_second: float


class OutboxSource(Protocol):
    def fetch(self, *, limit: int) -> list[OutboxRow]: ...

    def delete(self, rows: list[OutboxRow]) -> None: ...

    def oldest_commit_ts(self) -> dt.datetime | None: ...


class BatchWriter(Protocol):
    def insert(self, rows: list[dict[str, Any]]) -> None: ...


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


class SpannerOutboxSource:
    """Read each key shard in order, then merge by commit timestamp."""

    def __init__(
        self,
        *,
        project: str,
        instance: str,
        database: str,
        shard_count: int = OUTBOX_SHARDS,
    ) -> None:
        from google.cloud import spanner
        from google.cloud.spanner_v1 import param_types

        self._database = (
            spanner.Client(project=project, disable_builtin_metrics=True)
            .instance(instance)
            .database(database)
        )
        self._pt = param_types
        self._shard_count = shard_count

    def fetch(self, *, limit: int) -> list[OutboxRow]:
        if limit < 1:
            return []
        # IDs are UUID-derived and uniformly sharded. The 2x headroom avoids
        # returning a short global batch from normal hash variance while
        # bounding each shard read.
        per_shard = max(1, math.ceil(limit / self._shard_count) * 2)
        rows: list[OutboxRow] = []
        # One consistent multi-use snapshot is required because the sharded
        # primary key needs one ordered query per shard. The SDK's default
        # single-use snapshot rejects the second query.
        with self._database.snapshot(multi_use=True) as snapshot:
            for shard in range(self._shard_count):
                values = snapshot.execute_sql(
                    "SELECT shard, commit_ts, event_id, payload "
                    "FROM tr_analytics_outbox "
                    "WHERE shard=@shard ORDER BY commit_ts, event_id LIMIT @limit",
                    params={"shard": shard, "limit": per_shard},
                    param_types={"shard": self._pt.INT64, "limit": self._pt.INT64},
                )
                rows.extend(
                    OutboxRow(
                        shard=int(row[0]),
                        commit_ts=_utc(row[1]),
                        event_id=str(row[2]),
                        payload=str(row[3]),
                    )
                    for row in values
                )
        rows.sort(key=lambda row: (row.commit_ts, row.shard, row.event_id))
        return rows[:limit]

    def delete(self, rows: list[OutboxRow]) -> None:
        if not rows:
            return
        from google.cloud.spanner_v1 import KeySet

        with self._database.batch() as batch:
            batch.delete(OUTBOX_TABLE, KeySet(keys=[list(row.key) for row in rows]))

    def oldest_commit_ts(self) -> dt.datetime | None:
        oldest: dt.datetime | None = None
        with self._database.snapshot(multi_use=True) as snapshot:
            for shard in range(self._shard_count):
                values = list(
                    snapshot.execute_sql(
                        "SELECT commit_ts "
                        "FROM tr_analytics_outbox "
                        "WHERE shard=@shard ORDER BY commit_ts LIMIT 1",
                        params={"shard": shard},
                        param_types={"shard": self._pt.INT64},
                    )
                )
                if values:
                    candidate = _utc(values[0][0])
                    oldest = candidate if oldest is None else min(oldest, candidate)
        return oldest


class ClickHouseWriter:
    """Large synchronous inserts; process success is the durable ack."""

    def __init__(
        self,
        *,
        password: str,
        database: str = CLICKHOUSE_DATABASE,
        table: str = CLICKHOUSE_TABLE,
    ) -> None:
        self._password = password
        self._database = database
        self._table = table

    def insert(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        payload = "\n".join(
            json.dumps(row, separators=(",", ":"), sort_keys=True) for row in rows
        ).encode("utf-8")
        command = [
            "clickhouse-client",
            "--user",
            "tr",
            "--password",
            self._password,
            "--database",
            self._database,
            "--query",
            f"INSERT INTO {self._table} FORMAT JSONEachRow",
        ]
        result = subprocess.run(  # noqa: S603 - fixed executable and argv, no shell
            command,
            input=payload,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[:1000]
            raise RuntimeError(f"ClickHouse insert failed: {detail}")


def normalise_outbox_payload(payload: str) -> dict[str, Any]:
    raw = json.loads(payload)
    if not isinstance(raw, dict):
        raise ValueError("outbox payload is not a JSON object")
    row = normalise(raw)
    if row is None:
        raise ValueError("outbox payload cannot be normalised")
    return row


def drain_once(
    source: OutboxSource,
    writer: BatchWriter,
    metrics: DrainMetrics,
    *,
    batch_size: int,
) -> DrainResult:
    """Insert then advance the durable cursor by deleting acknowledged rows."""
    rows = source.fetch(limit=batch_size)
    if not rows:
        return DrainResult(fetched=0, inserted=0, rows_per_second=0.0)
    canonical = [normalise_outbox_payload(row.payload) for row in rows]
    started = time.monotonic()
    try:
        writer.insert(canonical)
    except Exception:
        metrics.clickhouse_insert_errors_total += 1
        raise
    # This is the cursor advance. It is intentionally after the acknowledged
    # insert; delete failure causes safe replay on the next pass.
    source.delete(rows)
    elapsed = max(time.monotonic() - started, 0.000_001)
    metrics.rows_ingested_total += len(rows)
    return DrainResult(
        fetched=len(rows),
        inserted=len(rows),
        rows_per_second=len(rows) / elapsed,
    )


def _lag_seconds(oldest: dt.datetime | None) -> float:
    if oldest is None:
        return 0.0
    return max(0.0, (dt.datetime.now(dt.UTC) - _utc(oldest)).total_seconds())


def _log_metrics(
    metrics: DrainMetrics,
    *,
    result: DrainResult,
    oldest: dt.datetime | None,
) -> None:
    log.info(
        "analytics_outbox.metrics rows=%d rows_per_second=%.3f "
        "drain_lag_seconds=%.3f clickhouse_insert_errors_total=%d "
        "rows_ingested_total=%d",
        result.inserted,
        result.rows_per_second,
        _lag_seconds(oldest),
        metrics.clickhouse_insert_errors_total,
        metrics.rows_ingested_total,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", PROJECT))
    parser.add_argument(
        "--spanner-instance",
        default=os.environ.get("SPANNER_INSTANCE_ID", SPANNER_INSTANCE),
    )
    parser.add_argument(
        "--spanner-database",
        default=os.environ.get("SPANNER_DATABASE_ID", SPANNER_DATABASE),
    )
    parser.add_argument("--shards", type=int, default=OUTBOX_SHARDS)
    parser.add_argument("--batch-size", type=int, default=5000)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--metrics-seconds", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    source = SpannerOutboxSource(
        project=args.project,
        instance=args.spanner_instance,
        database=args.spanner_database,
        shard_count=args.shards,
    )
    writer = ClickHouseWriter(password=os.environ["CH_PASSWORD"])
    metrics = DrainMetrics()
    last_metrics = 0.0

    log.info(
        "analytics_outbox.started project=%s instance=%s database=%s shards=%d batch_size=%d",
        args.project,
        args.spanner_instance,
        args.spanner_database,
        args.shards,
        args.batch_size,
    )
    while True:
        result = DrainResult(fetched=0, inserted=0, rows_per_second=0.0)
        try:
            result = drain_once(source, writer, metrics, batch_size=args.batch_size)
        except Exception:
            log.exception(
                "analytics_outbox.drain_failed clickhouse_insert_errors_total=%d",
                metrics.clickhouse_insert_errors_total,
            )
        now = time.monotonic()
        if result.inserted or now - last_metrics >= args.metrics_seconds:
            try:
                oldest = source.oldest_commit_ts()
            except Exception:
                log.exception("analytics_outbox.lag_measurement_failed")
                oldest = None
            _log_metrics(metrics, result=result, oldest=oldest)
            last_metrics = now
        if args.once:
            return 0
        if not result.inserted:
            time.sleep(max(0.1, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
