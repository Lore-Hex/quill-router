from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from clickhouse.archive_daily import (
    ArchiveStore,
    ExportedPart,
    SourceFingerprint,
    _combine_parts,
    _days_to_archive,
    archive_day,
)


class FakeExporter:
    def __init__(self, fingerprint: SourceFingerprint) -> None:
        self.fingerprint = fingerprint
        self.export_calls = 0
        self.parity_delta = 0

    def source_fingerprint(self, _day: dt.date) -> SourceFingerprint:
        return self.fingerprint

    def export_parts(
        self,
        _day: dt.date,
        destination: Path,
        *,
        part_count: int,
    ) -> list[Path]:
        self.export_calls += 1
        paths: list[Path] = []
        rows_left = self.fingerprint.rows
        hash_sum_left = self.fingerprint.hash_sum + self.parity_delta
        hash_xor_left = self.fingerprint.hash_xor
        for index in range(part_count):
            remaining_parts = part_count - index
            rows = rows_left // remaining_parts
            hash_sum = hash_sum_left // remaining_parts
            hash_xor = hash_xor_left if index == 0 else 0
            path = destination / f"part-{index:05d}-of-{part_count:05d}.parquet"
            path.write_text(json.dumps({"rows": rows, "hash_sum": hash_sum, "hash_xor": hash_xor}))
            paths.append(path)
            rows_left -= rows
            hash_sum_left -= hash_sum
        return paths

    def verify_part(self, path: Path) -> ExportedPart:
        value = json.loads(path.read_text())
        return ExportedPart(
            path=path,
            rows=int(value["rows"]),
            hash_sum=int(value["hash_sum"]),
            hash_xor=int(value["hash_xor"]),
        )


class MemoryStore(ArchiveStore):
    def __init__(self) -> None:
        self.json: dict[str, dict[str, Any]] = {}
        self.files: dict[str, bytes] = {}
        self.pointer_writes = 0

    def read_json(self, key: str) -> dict[str, Any] | None:
        return self.json.get(key)

    def put_file_if_absent(
        self,
        key: str,
        path: Path,
        *,
        sha256: str,
        metadata: dict[str, str],
    ) -> None:
        del sha256, metadata
        value = path.read_bytes()
        if key in self.files and self.files[key] != value:
            raise RuntimeError("immutable file differs")
        self.files[key] = value

    def put_json_if_absent(self, key: str, value: dict[str, Any]) -> None:
        if key in self.json and self.json[key] != value:
            raise RuntimeError("immutable manifest differs")
        self.json[key] = value

    def put_json_pointer(self, key: str, value: dict[str, Any]) -> None:
        self.pointer_writes += 1
        self.json[key] = value


def _fingerprint(*, rows: int = 7, hash_sum: int = 42, hash_xor: int = 17) -> SourceFingerprint:
    return SourceFingerprint(
        rows=rows,
        hash_sum=hash_sum,
        hash_xor=hash_xor,
        min_created_at="2026-07-01 00:00:01.000",
        max_created_at="2026-07-01 23:59:59.000",
    )


def test_archive_publishes_manifest_only_after_verified_parts() -> None:
    exporter = FakeExporter(_fingerprint())
    store = MemoryStore()

    result = archive_day(
        exporter,
        store,
        dt.date(2026, 7, 1),
        rows_per_part=3,
        now=dt.datetime(2026, 7, 3, tzinfo=dt.UTC),
    )

    assert result.skipped is False
    assert exporter.export_calls == 1
    assert len(store.files) == 3
    manifest = store.json[result.manifest_key]
    assert manifest["parquet_rows"] == 7
    assert len(manifest["parts"]) == 3
    assert all(len(part["sha256"]) == 64 for part in manifest["parts"])
    pointer = store.json["raw/provider_benchmark_samples/day=2026-07-01/_latest.json"]
    assert pointer["manifest"] == result.manifest_key
    assert store.pointer_writes == 1


def test_unchanged_source_skips_export_and_pointer_write() -> None:
    exporter = FakeExporter(_fingerprint())
    store = MemoryStore()
    first = archive_day(exporter, store, dt.date(2026, 7, 1), rows_per_part=10)
    second = archive_day(exporter, store, dt.date(2026, 7, 1), rows_per_part=10)

    assert first.revision == second.revision
    assert second.skipped is True
    assert exporter.export_calls == 1
    assert store.pointer_writes == 1


def test_late_rows_create_new_revision_without_overwriting_old_one() -> None:
    exporter = FakeExporter(_fingerprint())
    store = MemoryStore()
    first = archive_day(exporter, store, dt.date(2026, 7, 1), rows_per_part=10)
    exporter.fingerprint = _fingerprint(rows=8, hash_sum=99, hash_xor=31)
    second = archive_day(exporter, store, dt.date(2026, 7, 1), rows_per_part=10)

    assert first.revision != second.revision
    assert first.manifest_key in store.json
    assert second.manifest_key in store.json
    assert store.pointer_writes == 2


def test_parity_failure_never_publishes_manifest_or_pointer() -> None:
    exporter = FakeExporter(_fingerprint())
    exporter.parity_delta = 1
    store = MemoryStore()

    with pytest.raises(RuntimeError, match="archive parity mismatch"):
        archive_day(exporter, store, dt.date(2026, 7, 1), rows_per_part=10)

    assert store.json == {}
    assert store.files == {}


def test_empty_day_publishes_a_zero_row_manifest_without_parts() -> None:
    exporter = FakeExporter(_fingerprint(rows=0, hash_sum=0, hash_xor=0))
    store = MemoryStore()
    result = archive_day(exporter, store, dt.date(2026, 7, 1))

    assert result.rows == 0
    assert store.json[result.manifest_key]["parts"] == []
    assert store.files == {}


def test_combined_part_hash_uses_uint64_overflow_and_xor() -> None:
    parts = [
        ExportedPart(Path("a"), 2, (1 << 64) - 1, 0b1010),
        ExportedPart(Path("b"), 3, 4, 0b1100),
    ]
    combined = _combine_parts(parts)
    assert combined.rows == 5
    assert combined.hash_sum == 3
    assert combined.hash_xor == 0b0110


def test_archive_date_selection_never_includes_open_current_day() -> None:
    explicit = dt.date(2026, 7, 9)
    assert _days_to_archive(date=explicit, lookback_days=0) == [explicit]
    with pytest.raises(ValueError, match="positive"):
        _days_to_archive(date=None, lookback_days=0)


def test_backfill_date_selection_includes_every_closed_day(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> FrozenDateTime:
            value = cls(2026, 7, 5, tzinfo=dt.UTC)
            return value if tz is None else value.astimezone(tz)

    monkeypatch.setattr("clickhouse.archive_daily.dt.datetime", FrozenDateTime)
    assert _days_to_archive(
        date=None,
        lookback_days=0,
        backfill_start=dt.date(2026, 7, 2),
    ) == [dt.date(2026, 7, 2), dt.date(2026, 7, 3), dt.date(2026, 7, 4)]
