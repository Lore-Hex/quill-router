"""Download and verify a closed-day ClickHouse archive revision.

This is a restore drill, not just an object-existence check. Every Parquet part
is downloaded, hash checked, parsed by ClickHouse Local, and recombined into the
same row fingerprint recorded when the source day was exported.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from clickhouse.archive_daily import (
    ARCHIVE_BUCKET,
    ARCHIVE_SCHEMA_VERSION,
    DATASETS,
    PROJECT,
    ExportedPart,
    SourceFingerprint,
    _combine_parts,
    _fingerprint_query,
    _parse_fingerprint,
    _run_clickhouse_local,
    _sha256,
    _sql_string,
)

log = logging.getLogger("trusted_router.analytics_archive_restore")


class RestoreStore(Protocol):
    def read_json(self, key: str) -> dict[str, Any] | None: ...

    def download_file(self, key: str, destination: Path) -> None: ...


class GCSRestoreStore:
    def __init__(self, *, project: str, bucket: str) -> None:
        from google.cloud import storage

        self._bucket = storage.Client(project=project).bucket(bucket)

    def read_json(self, key: str) -> dict[str, Any] | None:
        blob = self._bucket.blob(key)
        if not blob.exists():
            return None
        value = json.loads(blob.download_as_text())
        if not isinstance(value, dict):
            raise RuntimeError(f"archive object {key} is not a JSON object")
        return value

    def download_file(self, key: str, destination: Path) -> None:
        self._bucket.blob(key).download_to_filename(str(destination))


@dataclasses.dataclass(frozen=True)
class RestoreResult:
    dataset: str
    day: dt.date
    rows: int
    parts: int
    revision: str


def verify_parquet_part(path: Path, *, dataset: str) -> ExportedPart:
    try:
        spec = DATASETS[dataset]
    except KeyError:
        raise ValueError(f"unsupported archive dataset: {dataset}") from None
    query = _fingerprint_query(
        f"file({_sql_string(str(path))}, Parquet)",
        where=None,
        final=False,
        columns=spec.columns,
        time_column=spec.time_column,
    )
    fingerprint = _parse_fingerprint(_run_clickhouse_local(query))
    return ExportedPart(
        path=path,
        rows=fingerprint.rows,
        hash_sum=fingerprint.hash_sum,
        hash_xor=fingerprint.hash_xor,
    )


def verify_archived_day(
    store: RestoreStore,
    *,
    dataset: str,
    day: dt.date,
    verifier: Callable[[Path], ExportedPart] | None = None,
) -> RestoreResult:
    if dataset not in DATASETS:
        raise ValueError(f"unsupported archive dataset: {dataset}")
    prefix = f"raw/{dataset}/day={day.isoformat()}"
    pointer = store.read_json(f"{prefix}/_latest.json")
    if pointer is None:
        raise RuntimeError(f"archive pointer missing for {dataset} on {day}")
    manifest_key = str(pointer.get("manifest") or "")
    if not manifest_key.startswith(f"{prefix}/revisions/"):
        raise RuntimeError("archive pointer references an unexpected manifest")
    manifest = store.read_json(manifest_key)
    if manifest is None:
        raise RuntimeError(f"archive manifest missing: {manifest_key}")
    if int(manifest.get("schema_version", -1)) != ARCHIVE_SCHEMA_VERSION:
        raise RuntimeError("archive schema version does not match the verifier")
    if manifest.get("dataset") != dataset or manifest.get("day") != day.isoformat():
        raise RuntimeError("archive manifest identity does not match the requested day")

    source = SourceFingerprint.from_dict(dict(manifest["source_fingerprint"]))
    pointer_source = SourceFingerprint.from_dict(dict(pointer["source_fingerprint"]))
    if not source.matches(pointer_source):
        raise RuntimeError("archive pointer and manifest fingerprints differ")
    parts_value = manifest.get("parts")
    if not isinstance(parts_value, list):
        raise RuntimeError("archive manifest parts must be a list")

    verified: list[ExportedPart] = []
    with tempfile.TemporaryDirectory(prefix=f"tr-restore-{dataset}-") as temporary:
        for index, raw_part in enumerate(parts_value):
            if not isinstance(raw_part, dict):
                raise RuntimeError("archive manifest part is not an object")
            key = str(raw_part.get("key") or "")
            if not key.startswith(f"{prefix}/revisions/"):
                raise RuntimeError("archive part is outside the requested dataset")
            path = Path(temporary) / f"part-{index:05d}.parquet"
            store.download_file(key, path)
            if _sha256(path) != str(raw_part.get("sha256") or ""):
                raise RuntimeError(f"archive SHA-256 mismatch: {key}")
            part = (verifier or (lambda value: verify_parquet_part(value, dataset=dataset)))(
                path
            )
            expected = (
                int(raw_part.get("rows", -1)),
                int(raw_part.get("hash_sum", -1)),
                int(raw_part.get("hash_xor", -1)),
            )
            if (part.rows, part.hash_sum, part.hash_xor) != expected:
                raise RuntimeError(f"archive Parquet fingerprint mismatch: {key}")
            verified.append(part)

    restored = _combine_parts(verified)
    if not source.matches(restored):
        raise RuntimeError("restored archive does not match the source fingerprint")
    return RestoreResult(
        dataset=dataset,
        day=day,
        rows=source.rows,
        parts=len(verified),
        revision=str(manifest.get("revision") or ""),
    )


def _write_result(path: Path, results: list[RestoreResult]) -> None:
    value = {
        "checked_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "ok": True,
        "datasets": [dataclasses.asdict(result) for result in results],
    }
    value["datasets"] = [
        {**row, "day": str(row["day"])} for row in value["datasets"]  # type: ignore[misc]
    ]
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, sort_keys=True) + "\n")
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default=os.environ.get("GCP_PROJECT_ID", PROJECT))
    parser.add_argument("--bucket", default=os.environ.get("ARCHIVE_BUCKET", ARCHIVE_BUCKET))
    parser.add_argument("--table", action="append", choices=tuple(DATASETS))
    parser.add_argument("--date", type=dt.date.fromisoformat)
    parser.add_argument(
        "--result-file",
        type=Path,
        default=Path("/var/lib/tr-clickhouse-ingest/archive-restore.json"),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    day = args.date or (dt.datetime.now(dt.UTC).date() - dt.timedelta(days=1))
    if day >= dt.datetime.now(dt.UTC).date():
        raise SystemExit("restore verification only accepts closed UTC days")
    store = GCSRestoreStore(project=args.project, bucket=args.bucket)
    results = [
        verify_archived_day(store, dataset=dataset, day=day)
        for dataset in tuple(dict.fromkeys(args.table or tuple(DATASETS)))
    ]
    _write_result(args.result_file, results)
    for result in results:
        log.info(
            "analytics_archive_restore.completed dataset=%s day=%s rows=%d "
            "parts=%d revision=%s",
            result.dataset,
            result.day,
            result.rows,
            result.parts,
            result.revision,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
