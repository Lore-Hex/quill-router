"""Prove every closed ClickHouse partition has a current archive pointer."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
from typing import Any, Protocol

from clickhouse.archive_daily import (
    ARCHIVE_BUCKET,
    DATABASE,
    DATASETS,
    PROJECT,
    ArchiveStore,
    ClickHouseDailyExporter,
    GCSArchiveStore,
    SourceFingerprint,
)


class BackfillExporter(Protocol):
    def earliest_day(self) -> dt.date | None: ...

    def source_fingerprint(self, day: dt.date) -> SourceFingerprint: ...


def _closed_days(first: dt.date, today: dt.date) -> list[dt.date]:
    if first >= today:
        return []
    return [first + dt.timedelta(days=offset) for offset in range((today - first).days)]


def _verified_restore(path: Path, *, now: dt.datetime) -> None:
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not value.get("ok"):
        raise RuntimeError("archive restore drill has not passed")
    checked_at = dt.datetime.fromisoformat(
        str(value["checked_at"]).replace("Z", "+00:00")
    )
    if checked_at.tzinfo is None:
        checked_at = checked_at.replace(tzinfo=dt.UTC)
    if now - checked_at.astimezone(dt.UTC) > dt.timedelta(hours=30):
        raise RuntimeError("archive restore drill is stale")
    found = {
        str(item["dataset"])
        for item in value.get("datasets", [])
        if isinstance(item, dict) and item.get("dataset")
    }
    if found != set(DATASETS):
        raise RuntimeError("archive restore drill does not cover every dataset")


def verify_archive_backfill(
    exporters: dict[str, BackfillExporter],
    store: ArchiveStore,
    *,
    now: dt.datetime,
    restore_result: Path,
) -> dict[str, Any]:
    _verified_restore(restore_result, now=now)
    today = now.astimezone(dt.UTC).date()
    coverage: dict[str, dict[str, Any]] = {}
    for dataset in DATASETS:
        exporter = exporters[dataset]
        first = exporter.earliest_day()
        days = [] if first is None else _closed_days(first, today)
        for day in days:
            source = exporter.source_fingerprint(day)
            prefix = f"raw/{dataset}/day={day.isoformat()}"
            pointer = store.read_json(f"{prefix}/_latest.json")
            if pointer is None:
                raise RuntimeError(f"archive pointer missing for {dataset} on {day}")
            archived = SourceFingerprint.from_dict(
                dict(pointer.get("source_fingerprint") or {})
            )
            if not source.matches(archived):
                raise RuntimeError(f"archive pointer is stale for {dataset} on {day}")
            manifest_key = str(pointer.get("manifest") or "")
            if not manifest_key.startswith(f"{prefix}/revisions/"):
                raise RuntimeError(f"archive manifest path is invalid for {dataset} on {day}")
            manifest = store.read_json(manifest_key)
            if (
                manifest is None
                or manifest.get("dataset") != dataset
                or manifest.get("day") != day.isoformat()
                or str(manifest.get("revision") or "")
                != str(pointer.get("revision") or "")
            ):
                raise RuntimeError(f"archive manifest is invalid for {dataset} on {day}")
            manifest_source = SourceFingerprint.from_dict(
                dict(manifest.get("source_fingerprint") or {})
            )
            if not source.matches(manifest_source):
                raise RuntimeError(f"archive manifest is stale for {dataset} on {day}")
        coverage[dataset] = {
            "first_day": first.isoformat() if first is not None else None,
            "last_day": days[-1].isoformat() if days else None,
            "days": len(days),
        }
    return {
        "checked_at": now.astimezone(dt.UTC).isoformat().replace("+00:00", "Z"),
        "ok": True,
        "datasets": list(DATASETS),
        "coverage": coverage,
    }


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, sort_keys=True) + "\n")
    os.replace(temporary, path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", PROJECT))
    parser.add_argument("--bucket", default=os.environ.get("ARCHIVE_BUCKET", ARCHIVE_BUCKET))
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument(
        "--restore-result",
        type=Path,
        default=Path("/var/lib/tr-clickhouse-ingest/archive-restore.json"),
    )
    parser.add_argument(
        "--result-file",
        type=Path,
        default=Path(
            "/var/lib/tr-clickhouse-ingest/archive-backfill-complete.json"
        ),
    )
    args = parser.parse_args()
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    exporters: dict[str, BackfillExporter] = {
        dataset: ClickHouseDailyExporter(
            password=password,
            database=args.database,
            table=dataset,
        )
        for dataset in DATASETS
    }
    result = verify_archive_backfill(
        exporters,
        GCSArchiveStore(project=args.project, bucket=args.bucket),
        now=dt.datetime.now(dt.UTC),
        restore_result=args.restore_result,
    )
    _write_result(args.result_file, result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
