"""Compare bounded Bigtable samples with their ClickHouse replicas."""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, TypeVar

from google.cloud import bigtable
from google.cloud.bigtable.row_filters import CellsColumnLimitFilter

from clickhouse.backfill_benchmark_samples import normalise as normalise_benchmark
from clickhouse.backfill_operational_analytics import ClickHouse
from clickhouse.ingest_operational_outbox import (
    OperationalOutboxRow,
    normalise_operational_event,
)
from trusted_router.storage_gcp_operational_analytics_outbox import (
    activity_payload,
    synthetic_payload,
)
from trusted_router.storage_models import (
    Generation,
    ProviderBenchmarkSample,
    SyntheticProbeSample,
    SyntheticRollup,
)

PROJECT = "quill-cloud-proxy"
INSTANCE = "trusted-router-logs"
TABLE = "trustedrouter-generations"
SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
T = TypeVar("T")


def _body(row: Any, families: tuple[str, ...]) -> dict[str, Any] | None:
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


def _iso(value: Any) -> str:
    text = str(value).replace(" ", "T")
    return text if text.endswith("Z") else text + "Z"


def _canonical(payload: dict[str, Any], *, surface: str) -> str:
    payload = dict(payload)
    payload.pop("ingest_version", None)
    payload.pop("updated_at", None)
    for field in ("created_at", "period_start", "last_checked_at"):
        if payload.get(field) is not None:
            payload[field] = _iso(payload[field])
    if surface == "synthetic":
        for field in ("connection_reused", "output_match"):
            if payload.get(field) is not None:
                payload[field] = bool(payload[field])
    if surface == "activity":
        for field in ("streamed", "usage_estimated"):
            if payload.get(field) is not None:
                payload[field] = bool(payload[field])
    if surface == "rollup" and payload.get("target_region") is None:
        payload["target_region"] = ""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _clickhouse_rows(
    clickhouse: ClickHouse,
    *,
    table: str,
    id_column: str,
    ids: list[str],
) -> dict[str, dict[str, Any]]:
    allowed = {
        ("provider_benchmark_samples", "id"),
        ("activity_generations", "generation_id"),
        ("synthetic_probe_samples", "id"),
        ("synthetic_status_rollups", "id"),
    }
    if (table, id_column) not in allowed:
        raise ValueError("unsupported parity table")
    if not ids:
        return {}
    if any(SAFE_ID.fullmatch(item) is None for item in ids):
        raise ValueError("source contains an invalid record ID")
    payload = ("\n".join(ids) + "\n").encode()
    result = clickhouse.query(
        f"SELECT * EXCEPT ingest_version FROM {table} FINAL "  # noqa: S608
        f"WHERE {id_column} IN (SELECT id FROM wanted) "
        "FORMAT JSONEachRow",
        input_bytes=payload,
        external_ids=True,
    )
    rows: dict[str, dict[str, Any]] = {}
    for line in result.splitlines():
        row = json.loads(line)
        if isinstance(row, dict):
            rows[str(row[id_column])] = row
    return rows


def _source_rows(table: Any, *, surface: str, limit: int) -> dict[str, dict[str, Any]]:
    config = {
        "benchmark": (b"benchmark_recent#", ("benchmark", "m")),
        "activity": (b"ws_recent#", ("activity", "m")),
        "synthetic": (b"synthetic_recent#", ("synthetic", "m")),
        "rollup": (b"synthetic_rollup#", ("rollup", "m")),
    }
    prefix, families = config[surface]
    rows = table.read_rows(
        start_key=prefix,
        end_key=prefix + b"~",
        limit=limit,
        filter_=CellsColumnLimitFilter(1),
    )
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        raw = _body(row, families)
        if surface == "benchmark":
            benchmark_sample = _parse(ProviderBenchmarkSample, raw)
            if benchmark_sample is None:
                continue
            normalized = normalise_benchmark(dataclasses.asdict(benchmark_sample))
            if normalized is not None:
                result[benchmark_sample.id] = normalized
        elif surface == "activity":
            generation = _parse(Generation, raw)
            if generation is None:
                continue
            event = normalise_operational_event(
                OperationalOutboxRow(
                    shard=0,
                    commit_ts=dt.datetime.now(dt.UTC),
                    event_kind="activity",
                    event_id=generation.id,
                    payload=json.dumps(activity_payload(generation)),
                )
            )
            event.row.pop("ingest_version", None)
            result[generation.id] = event.row
        elif surface == "synthetic":
            synthetic_sample = _parse(SyntheticProbeSample, raw)
            if synthetic_sample is not None:
                result[synthetic_sample.id] = synthetic_payload(synthetic_sample)
        else:
            rollup = _parse(SyntheticRollup, raw)
            if rollup is not None:
                result[rollup.id] = dataclasses.asdict(rollup)
    return result


def compare_surface(
    clickhouse: ClickHouse,
    table: Any,
    *,
    surface: str,
    limit: int,
) -> dict[str, Any]:
    destinations = {
        "benchmark": ("provider_benchmark_samples", "id"),
        "activity": ("activity_generations", "generation_id"),
        "synthetic": ("synthetic_probe_samples", "id"),
        "rollup": ("synthetic_status_rollups", "id"),
    }
    source = _source_rows(table, surface=surface, limit=limit)
    ch_table, id_column = destinations[surface]
    destination = _clickhouse_rows(
        clickhouse,
        table=ch_table,
        id_column=id_column,
        ids=list(source),
    )
    missing = 0
    mismatched = 0
    for record_id, source_row in source.items():
        destination_row = destination.get(record_id)
        if destination_row is None:
            missing += 1
        elif _canonical(source_row, surface=surface) != _canonical(
            destination_row,
            surface=surface,
        ):
            mismatched += 1
    return {
        "sampled": len(source),
        "found": len(destination),
        "missing": missing,
        "mismatched": mismatched,
        "ok": missing == 0 and mismatched == 0,
    }


def _write_history(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cutoff = dt.datetime.now(dt.UTC) - dt.timedelta(days=16)
    history: list[dict[str, Any]] = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                row = json.loads(line)
                created = dt.datetime.fromisoformat(
                    str(row["checked_at"]).replace("Z", "+00:00")
                )
            except (KeyError, TypeError, ValueError):
                continue
            if created >= cutoff:
                history.append(row)
    history.append(result)
    temporary = path.with_suffix(".tmp")
    temporary.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in history))
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument(
        "--history-file",
        default="/var/lib/tr-clickhouse-ingest/operational-parity.jsonl",
    )
    args = parser.parse_args()
    if args.limit < 1:
        raise SystemExit("--limit must be positive")
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    table = (
        bigtable.Client(project=PROJECT, admin=False)
        .instance(INSTANCE)
        .table(TABLE)
    )
    clickhouse = ClickHouse(password=password)
    surfaces = {
        surface: compare_surface(
            clickhouse,
            table,
            surface=surface,
            limit=args.limit,
        )
        for surface in ("benchmark", "activity", "synthetic", "rollup")
    }
    result = {
        "checked_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "ok": all(item["ok"] for item in surfaces.values()),
        "surfaces": surfaces,
    }
    _write_history(Path(args.history_file), result)
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
