"""Drain tenant activity and synthetic metadata from Spanner to ClickHouse."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Protocol

PROJECT = "quill-cloud-proxy"
SPANNER_INSTANCE = "trusted-router-nam6"
SPANNER_DATABASE = "trusted-router"
OUTBOX_TABLE = "tr_operational_analytics_outbox"
OUTBOX_SHARDS = 32

ACTIVITY_COLUMNS = (
    "generation_id",
    "request_id",
    "tenant_id",
    "key_id",
    "model",
    "provider",
    "provider_name",
    "app",
    "tokens_prompt",
    "tokens_completion",
    "cached_input_tokens",
    "reasoning_tokens",
    "total_cost_microdollars",
    "usage_type",
    "speed_tokens_per_second",
    "finish_reason",
    "status",
    "streamed",
    "usage_estimated",
    "elapsed_milliseconds",
    "first_token_milliseconds",
    "ttfb_milliseconds",
    "region",
    "user",
    "session_id",
    "http_referer",
    "app_categories",
    "tags",
    "created_at",
)
SYNTHETIC_COLUMNS = (
    "id",
    "probe_type",
    "target",
    "target_url",
    "monitor_region",
    "status",
    "target_region",
    "latency_milliseconds",
    "ttfb_milliseconds",
    "dns_milliseconds",
    "tcp_connect_milliseconds",
    "tls_handshake_milliseconds",
    "gateway_processing_milliseconds",
    "connection_reused",
    "protocol",
    "http_status",
    "error_type",
    "provider",
    "model",
    "selected_provider",
    "selected_model",
    "generation_id",
    "attestation_digest",
    "source_commit",
    "cost_microdollars",
    "output_match",
    "created_at",
)

EVENT_TABLES = {
    "activity": "activity_generations",
    "synthetic": "synthetic_probe_samples",
}

log = logging.getLogger("trusted_router.operational_analytics_ingest")


@dataclass(frozen=True)
class OperationalOutboxRow:
    shard: int
    commit_ts: dt.datetime
    event_kind: str
    event_id: str
    payload: str

    @property
    def key(self) -> tuple[int, dt.datetime, str, str]:
        return (self.shard, self.commit_ts, self.event_kind, self.event_id)


@dataclass(frozen=True)
class CanonicalOperationalEvent:
    event_kind: str
    row: dict[str, Any]


@dataclass(frozen=True)
class DrainResult:
    fetched: int
    inserted: int
    rows_per_second: float


class OutboxSource(Protocol):
    def fetch(self, *, limit: int) -> list[OperationalOutboxRow]: ...

    def delete(self, rows: list[OperationalOutboxRow]) -> None: ...

    def oldest_commit_ts(self) -> dt.datetime | None: ...


class BatchWriter(Protocol):
    def insert(self, events: list[CanonicalOperationalEvent]) -> None: ...


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


class SpannerOperationalOutboxSource:
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

    def fetch(self, *, limit: int) -> list[OperationalOutboxRow]:
        if limit < 1:
            return []
        per_shard = max(1, math.ceil(limit / self._shard_count) * 2)
        rows: list[OperationalOutboxRow] = []
        with self._database.snapshot(multi_use=True) as snapshot:
            for shard in range(self._shard_count):
                values = snapshot.execute_sql(
                    "SELECT shard, commit_ts, event_kind, event_id, payload "
                    "FROM tr_operational_analytics_outbox "
                    "WHERE shard=@shard ORDER BY commit_ts, event_kind, event_id "
                    "LIMIT @limit",
                    params={"shard": shard, "limit": per_shard},
                    param_types={"shard": self._pt.INT64, "limit": self._pt.INT64},
                )
                rows.extend(
                    OperationalOutboxRow(
                        shard=int(row[0]),
                        commit_ts=_utc(row[1]),
                        event_kind=str(row[2]),
                        event_id=str(row[3]),
                        payload=str(row[4]),
                    )
                    for row in values
                )
        rows.sort(
            key=lambda row: (
                row.commit_ts,
                row.shard,
                row.event_kind,
                row.event_id,
            )
        )
        return rows[:limit]

    def delete(self, rows: list[OperationalOutboxRow]) -> None:
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
                        "SELECT commit_ts FROM tr_operational_analytics_outbox "
                        "WHERE shard=@shard ORDER BY commit_ts LIMIT 1",
                        params={"shard": shard},
                        param_types={"shard": self._pt.INT64},
                    )
                )
                if values:
                    candidate = _utc(values[0][0])
                    oldest = candidate if oldest is None else min(oldest, candidate)
        return oldest


class ClickHouseOperationalWriter:
    def __init__(self, *, password: str, database: str = "tr") -> None:
        self._password = password
        self._database = database

    def insert(self, events: list[CanonicalOperationalEvent]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for event in events:
            grouped[event.event_kind].append(event.row)
        for event_kind, rows in grouped.items():
            table = EVENT_TABLES.get(event_kind)
            if table is None:
                raise ValueError(f"unsupported operational event kind: {event_kind}")
            payload = "\n".join(
                json.dumps(row, separators=(",", ":"), sort_keys=True) for row in rows
            ).encode("utf-8")
            command = [
                "/usr/bin/clickhouse-client",
                "--user",
                "tr",
                "--database",
                self._database,
                "--query",
                f"INSERT INTO {table} FORMAT JSONEachRow",
            ]
            env = os.environ.copy()
            env["CLICKHOUSE_PASSWORD"] = self._password
            result = subprocess.run(  # noqa: S603 - fixed executable and table allowlist.
                command,
                input=payload,
                env=env,
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                detail = result.stderr.decode("utf-8", errors="replace")[:1000]
                raise RuntimeError(f"ClickHouse {event_kind} insert failed: {detail}")


def normalise_operational_event(row: OperationalOutboxRow) -> CanonicalOperationalEvent:
    raw = json.loads(row.payload)
    if not isinstance(raw, dict):
        raise ValueError("operational outbox payload is not a JSON object")
    allowed: tuple[str, ...]
    if row.event_kind == "activity":
        allowed = ACTIVITY_COLUMNS
    elif row.event_kind == "synthetic":
        allowed = SYNTHETIC_COLUMNS
    else:
        raise ValueError(f"unsupported operational event kind: {row.event_kind}")
    missing = [column for column in allowed if column not in raw]
    if missing:
        raise ValueError(
            f"{row.event_kind} payload missing required fields: {', '.join(missing)}"
        )
    canonical = {column: raw[column] for column in allowed}
    canonical["ingest_version"] = _utc(row.commit_ts).isoformat()
    return CanonicalOperationalEvent(event_kind=row.event_kind, row=canonical)


def drain_once(
    source: OutboxSource,
    writer: BatchWriter,
    *,
    batch_size: int,
) -> DrainResult:
    rows = source.fetch(limit=batch_size)
    if not rows:
        return DrainResult(fetched=0, inserted=0, rows_per_second=0.0)
    events = [normalise_operational_event(row) for row in rows]
    started = time.monotonic()
    writer.insert(events)
    source.delete(rows)
    elapsed = max(time.monotonic() - started, 0.000_001)
    return DrainResult(
        fetched=len(rows),
        inserted=len(events),
        rows_per_second=len(events) / elapsed,
    )


def _lag_seconds(oldest: dt.datetime | None) -> float:
    if oldest is None:
        return 0.0
    return max(0.0, (dt.datetime.now(dt.UTC) - _utc(oldest)).total_seconds())


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
    parser.add_argument("--poll-seconds", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    source = SpannerOperationalOutboxSource(
        project=args.project,
        instance=args.spanner_instance,
        database=args.spanner_database,
        shard_count=args.shards,
    )
    writer = ClickHouseOperationalWriter(password=password)
    while True:
        result = drain_once(source, writer, batch_size=max(1, args.batch_size))
        log.info(
            "operational_analytics_outbox.metrics rows=%d rows_per_second=%.3f "
            "drain_lag_seconds=%.3f",
            result.inserted,
            result.rows_per_second,
            _lag_seconds(source.oldest_commit_ts()),
        )
        if args.once:
            return 0
        if result.fetched == 0:
            time.sleep(max(0.1, args.poll_seconds))


if __name__ == "__main__":
    raise SystemExit(main())
