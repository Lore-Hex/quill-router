"""Replay bounded Bigtable operational metadata into replicated ClickHouse."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import subprocess
import sys
from collections.abc import Iterable, Iterator
from typing import Any, TypeVar

from google.cloud import bigtable
from google.cloud.bigtable.row_filters import CellsColumnLimitFilter

from clickhouse.ingest_operational_outbox import (
    CanonicalOperationalEvent,
    ClickHouseOperationalWriter,
    OperationalOutboxRow,
    normalise_operational_event,
)
from trusted_router.storage_gcp_operational_analytics_outbox import (
    activity_payload,
    synthetic_payload,
)
from trusted_router.storage_models import Generation, SyntheticProbeSample, SyntheticRollup
from trusted_router.synthetic.rollups import compact_histogram

ROLLUP_HISTOGRAM_FIELDS = (
    "latency_histogram",
    "ttfb_histogram",
    "dns_histogram",
    "tcp_connect_histogram",
    "tls_handshake_histogram",
    "gateway_processing_histogram",
)

PROJECT = "quill-cloud-proxy"
INSTANCE = "trusted-router-logs"
TABLE = "trustedrouter-generations"
DATABASE = "tr"
FAMILIES = {
    "activity": ("activity", "m"),
    "synthetic": ("synthetic", "m"),
    "rollup": ("rollup", "m"),
}
T = TypeVar("T")


class ClickHouse:
    def __init__(self, *, password: str) -> None:
        self._password = password

    def query(
        self,
        sql: str,
        *,
        input_bytes: bytes | None = None,
        external_ids: bool = False,
    ) -> str:
        env = os.environ.copy()
        env["CLICKHOUSE_PASSWORD"] = self._password
        command = [
            "/usr/bin/clickhouse-client",
            "--user",
            "tr",
            "--database",
            DATABASE,
        ]
        if external_ids:
            command.extend(
                [
                    "--external",
                    "--file",
                    "-",
                    "--name",
                    "wanted",
                    "--structure",
                    "id String",
                    "--format",
                    "TabSeparated",
                ]
            )
        command.extend(["--query", sql])
        result = subprocess.run(  # noqa: S603 - fixed executable and SQL below.
            command,
            input=input_bytes,
            env=env,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace")[:1000]
            raise RuntimeError(f"ClickHouse query failed: {detail}")
        return result.stdout.decode()


def _body(row: Any, families: Iterable[str]) -> dict[str, Any] | None:
    for family in families:
        cells = row.cells.get(family, {}).get(b"body", [])
        if not cells:
            continue
        try:
            payload = json.loads(cells[0].value.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None
    return None


def _parse(cls: type[T], payload: dict[str, Any] | None) -> T | None:
    if payload is None:
        return None
    try:
        return cls(**payload)
    except (TypeError, ValueError):
        return None


def _created_at(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def iter_activity_events(
    table: Any,
    *,
    limit: int | None = None,
) -> Iterator[CanonicalOperationalEvent]:
    rows = table.read_rows(
        start_key=b"ws_recent#",
        end_key=b"ws_recent#~",
        limit=limit,
        filter_=CellsColumnLimitFilter(1),
    )
    for row in rows:
        generation = _parse(Generation, _body(row, FAMILIES["activity"]))
        if generation is None:
            continue
        yield from normalise_operational_event(
            OperationalOutboxRow(
                shard=0,
                commit_ts=_created_at(generation.created_at),
                event_kind="activity",
                event_id=generation.id,
                payload=json.dumps(activity_payload(generation)),
            )
        )


def iter_synthetic_events(
    table: Any,
    *,
    limit: int | None = None,
) -> Iterator[CanonicalOperationalEvent]:
    rows = table.read_rows(
        start_key=b"synthetic_recent#",
        end_key=b"synthetic_recent#~",
        limit=limit,
        filter_=CellsColumnLimitFilter(1),
    )
    for row in rows:
        sample = _parse(SyntheticProbeSample, _body(row, FAMILIES["synthetic"]))
        if sample is None:
            continue
        yield from normalise_operational_event(
            OperationalOutboxRow(
                shard=0,
                commit_ts=_created_at(sample.created_at),
                event_kind="synthetic",
                event_id=sample.id,
                payload=json.dumps(synthetic_payload(sample)),
            )
        )


def iter_rollups(table: Any) -> Iterator[SyntheticRollup]:
    rows = table.read_rows(
        start_key=b"synthetic_rollup#",
        end_key=b"synthetic_rollup#~",
        filter_=CellsColumnLimitFilter(1),
    )
    for row in rows:
        rollup = _parse(SyntheticRollup, _body(row, FAMILIES["rollup"]))
        if rollup is not None:
            yield rollup


def _batches(items: Iterable[T], size: int) -> Iterator[list[T]]:
    batch: list[T] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def insert_rollups(clickhouse: ClickHouse, rollups: list[SyntheticRollup]) -> None:
    if not rollups:
        return
    version = dt.datetime.now(dt.UTC).isoformat()
    payload = b"\n".join(
        json.dumps(
            {
                **dataclasses.asdict(rollup),
                # Legacy Bigtable bodies keep one key per millisecond until
                # their next write; never carry that shape into ClickHouse.
                **{
                    field: compact_histogram(getattr(rollup, field))
                    for field in ROLLUP_HISTOGRAM_FIELDS
                },
                "target_region": rollup.target_region or "",
                "ingest_version": version,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        for rollup in rollups
    )
    clickhouse.query(
        "INSERT INTO synthetic_status_rollups FORMAT JSONEachRow",
        input_bytes=payload,
    )


def _count(clickhouse: ClickHouse, table: str) -> int:
    allowed = {
        "activity_generations",
        "synthetic_probe_samples",
        "synthetic_status_rollups",
    }
    if table not in allowed:
        raise ValueError("unsupported table")
    return int(clickhouse.query(f"SELECT count() FROM {table} FINAL").strip())  # noqa: S608


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--batch", type=int, default=5000)
    parser.add_argument("--recent-limit", type=int)
    parser.add_argument("--skip-activity", action="store_true")
    parser.add_argument("--skip-synthetic", action="store_true")
    parser.add_argument("--skip-rollups", action="store_true")
    args = parser.parse_args()
    if args.batch < 1:
        raise SystemExit("--batch must be positive")
    if args.recent_limit is not None and args.recent_limit < 1:
        raise SystemExit("--recent-limit must be positive")

    table = bigtable.Client(project=PROJECT, admin=False).instance(INSTANCE).table(TABLE)
    password = os.environ.get("CH_PASSWORD", "")
    if args.apply and not password:
        raise SystemExit("CH_PASSWORD is required with --apply")

    clickhouse = ClickHouse(password=password) if args.apply else None
    writer = (
        ClickHouseOperationalWriter(password=password, database=DATABASE) if args.apply else None
    )
    counts = {"activity": 0, "synthetic": 0, "rollup": 0}

    sources = []
    if not args.skip_activity:
        sources.append(("activity", iter_activity_events(table, limit=args.recent_limit)))
    if not args.skip_synthetic:
        sources.append(("synthetic", iter_synthetic_events(table, limit=args.recent_limit)))
    for kind, events in sources:
        for batch in _batches(events, args.batch):
            counts[kind] += len(batch)
            if writer is not None:
                writer.insert(batch)
            print(f"{kind}: {counts[kind]} rows", file=sys.stderr, flush=True)

    if not args.skip_rollups:
        for rollup_batch in _batches(iter_rollups(table), args.batch):
            counts["rollup"] += len(rollup_batch)
            if clickhouse is not None:
                insert_rollups(clickhouse, rollup_batch)
            print(f"rollup: {counts['rollup']} rows", file=sys.stderr, flush=True)

    result: dict[str, Any] = {"mode": "apply" if args.apply else "dry-run", **counts}
    if clickhouse is not None:
        result["clickhouse"] = {
            "activity": _count(clickhouse, "activity_generations"),
            "synthetic": _count(clickhouse, "synthetic_probe_samples"),
            "rollup": _count(clickhouse, "synthetic_status_rollups"),
        }
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
