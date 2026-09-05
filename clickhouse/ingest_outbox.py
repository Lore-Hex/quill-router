"""Continuously drain the Spanner analytics outbox into local ClickHouse.

The durable cursor is the outbox itself: ClickHouse must acknowledge a batch
before its exact Spanner primary keys are deleted. A crash between those two
operations replays the batch, which is safe because canonical queries use
``FINAL`` over a ReplacingMergeTree.

Per-process, per-shard acknowledged commit timestamps are only scan optimisations;
deletion remains the authoritative cursor. Strong reads of commit-timestamped,
immutable enqueues make an inclusive floor safe, including timestamp ties and
batches truncated at the global limit. Restart or any drain error clears all
floors and restores full scans.

Empty passes back off from ``--poll-seconds`` (minimum 0.1 s), doubling to
``TR_OUTBOX_IDLE_MAX_SECONDS`` (default 5 s). The cap must be at least the base
interval. Backoff adds at most that cap of waiting to delivery latency, excluding
query/insert time and retries. Reading rows resets the interval; active batches
drain immediately. Lag metrics use the oldest row from the pass's fetch snapshot,
before deletion, without a separate probe.
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
    oldest_commit_ts: dt.datetime | None = None


class OutboxSource(Protocol):
    def fetch(self, *, limit: int) -> list[OutboxRow]: ...

    def delete(self, rows: list[OutboxRow]) -> None: ...

    def reset_scan_floors(self) -> None: ...


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
        self._floors: dict[int, dt.datetime] = {}

    def reset_scan_floors(self) -> None:
        self._floors.clear()

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
                params: dict[str, Any] = {"shard": shard, "limit": per_shard}
                param_types = {"shard": self._pt.INT64, "limit": self._pt.INT64}
                predicate = "WHERE shard=@shard"
                if shard in self._floors:
                    predicate += " AND commit_ts >= @floor"
                    params["floor"] = self._floors[shard]
                    param_types["floor"] = self._pt.TIMESTAMP
                values = snapshot.execute_sql(
                    "SELECT shard, commit_ts, event_id, payload "  # noqa: S608 - fixed SQL; values bound
                    "FROM tr_analytics_outbox "
                    f"{predicate} ORDER BY commit_ts, event_id LIMIT @limit",
                    params=params,
                    param_types=param_types,
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

        # Only advance after the batch context has committed the exact deletes.
        for row in rows:
            self._floors[row.shard] = max(
                self._floors.get(row.shard, row.commit_ts), row.commit_ts
            )


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
    try:
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
            oldest_commit_ts=min(row.commit_ts for row in rows),
        )
    except Exception:
        source.reset_scan_floors()
        raise


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
    parser.add_argument(
        "--idle-max-seconds",
        type=float,
        default=os.environ.get("TR_OUTBOX_IDLE_MAX_SECONDS", "5"),
        help="maximum idle wait (seconds); must be >= the poll interval",
    )
    parser.add_argument("--metrics-seconds", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    poll_seconds = max(0.1, args.poll_seconds)
    if (
        not math.isfinite(args.poll_seconds)
        or not math.isfinite(args.idle_max_seconds)
        or args.idle_max_seconds < poll_seconds
    ):
        parser.error("idle max must be finite and >= the finite poll interval (minimum 0.1 s)")
    idle_seconds = poll_seconds

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
        failed = False
        try:
            result = drain_once(source, writer, metrics, batch_size=args.batch_size)
        except Exception:
            failed = True
            idle_seconds = poll_seconds
            log.exception(
                "analytics_outbox.drain_failed clickhouse_insert_errors_total=%d",
                metrics.clickhouse_insert_errors_total,
            )
        now = time.monotonic()
        if result.inserted or now - last_metrics >= args.metrics_seconds:
            _log_metrics(metrics, result=result, oldest=result.oldest_commit_ts)
            last_metrics = now
        if args.once:
            return 0
        if result.fetched:
            idle_seconds = poll_seconds
        else:
            time.sleep(idle_seconds)
            if not failed:
                idle_seconds = min(args.idle_max_seconds, idle_seconds * 2)


if __name__ == "__main__":
    raise SystemExit(main())
