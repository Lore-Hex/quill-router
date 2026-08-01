"""Verify typed Spanner generation records reached ClickHouse exactly."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Protocol

from clickhouse.backfill_operational_analytics import ClickHouse
from clickhouse.operational_fingerprint import canonical_fingerprint, clickhouse_rows
from trusted_router.storage_gcp_operational_analytics_outbox import activity_payload
from trusted_router.storage_models import Generation

PROJECT = "quill-cloud-proxy"
SPANNER_INSTANCE = "trusted-router-nam6"
SPANNER_DATABASE = "trusted-router"


class GenerationSource(Protocol):
    def fetch(
        self,
        *,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
    ) -> list[Generation]: ...


class SpannerGenerationSource:
    def __init__(self, *, project: str, instance: str, database: str) -> None:
        from google.cloud import spanner
        from google.cloud.spanner_v1 import param_types

        self._database = (
            spanner.Client(project=project, disable_builtin_metrics=True)
            .instance(instance)
            .database(database)
        )
        self._pt = param_types

    def fetch(
        self,
        *,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
    ) -> list[Generation]:
        with self._database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                "SELECT payload FROM "
                "tr_generation@{FORCE_INDEX=tr_generation_by_terminal_at} "
                "WHERE terminal_at >= @start AND terminal_at < @end "
                "ORDER BY terminal_at DESC LIMIT @limit",
                params={"start": start, "end": end, "limit": limit},
                param_types={
                    "start": self._pt.TIMESTAMP,
                    "end": self._pt.TIMESTAMP,
                    "limit": self._pt.INT64,
                },
            )
            return [_generation(str(row[0])) for row in rows]


def _generation(payload: str) -> Generation:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise RuntimeError("typed generation payload is not an object")
    known = {field.name for field in dataclasses.fields(Generation)}
    return Generation(**{key: item for key, item in value.items() if key in known})


def verify_delivery(
    source: GenerationSource,
    clickhouse: Any,
    *,
    start: dt.datetime,
    end: dt.datetime,
    limit: int,
) -> dict[str, Any]:
    generations = source.fetch(start=start, end=end, limit=limit)
    expected = {generation.id: activity_payload(generation) for generation in generations}
    actual = clickhouse_rows(
        clickhouse,
        table="activity_generations",
        id_column="generation_id",
        ids=list(expected),
    )
    missing = sorted(set(expected) - set(actual))
    mismatched = sorted(
        generation_id
        for generation_id in set(expected) & set(actual)
        if canonical_fingerprint(expected[generation_id], surface="activity")
        != canonical_fingerprint(actual[generation_id], surface="activity")
    )
    return {
        "sampled": len(expected),
        "found": len(actual),
        "missing": len(missing),
        "mismatched": len(mismatched),
        "missing_ids": missing[:20],
        "mismatched_ids": mismatched[:20],
        "ok": not missing and not mismatched,
    }


def _write_history(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=16)
    history: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                checked = dt.datetime.fromisoformat(
                    str(row["checked_at"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if checked >= cutoff:
                history.append(row)
    history.append(result)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in history))
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--lookback-hours", type=int, default=24)
    parser.add_argument("--grace-seconds", type=int, default=120)
    parser.add_argument(
        "--history-file",
        type=Path,
        default=Path("/var/lib/tr-clickhouse-ingest/spanner-delivery.jsonl"),
    )
    args = parser.parse_args()
    if args.limit < 1 or args.lookback_hours < 1 or args.grace_seconds < 0:
        raise SystemExit("limit/lookback must be positive and grace cannot be negative")
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    end = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=args.grace_seconds)
    start = end - dt.timedelta(hours=args.lookback_hours)
    result = verify_delivery(
        SpannerGenerationSource(
            project=os.environ.get("GCP_PROJECT_ID", PROJECT),
            instance=os.environ.get("SPANNER_INSTANCE_ID", SPANNER_INSTANCE),
            database=os.environ.get("SPANNER_DATABASE_ID", SPANNER_DATABASE),
        ),
        ClickHouse(password=password),
        start=start,
        end=end,
        limit=args.limit,
    )
    result["checked_at"] = dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z")
    result["window_start"] = start.isoformat().replace("+00:00", "Z")
    result["window_end"] = end.isoformat().replace("+00:00", "Z")
    _write_history(args.history_file, result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
