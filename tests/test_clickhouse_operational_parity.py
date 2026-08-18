from __future__ import annotations

import dataclasses
import datetime as dt
import json
from types import SimpleNamespace

from clickhouse.verify_operational_parity import (
    _canonical,
    _source_rollups_from_raw,
    _source_rows,
    _stable_source_row,
    _stable_source_write,
)
from trusted_router.storage_models import SyntheticProbeSample


def test_activity_parity_uses_clickhouse_millisecond_precision() -> None:
    source = {
        "generation_id": "gen_one",
        "created_at": "2026-07-31T17:00:00.123987Z",
        "streamed": True,
        "usage_estimated": False,
    }
    destination = {
        "generation_id": "gen_one",
        "created_at": "2026-07-31 17:00:00.123",
        "streamed": 1,
        "usage_estimated": 0,
    }
    assert _canonical(source, surface="activity") == _canonical(
        destination,
        surface="activity",
    )


def test_benchmark_parity_uses_clickhouse_float32_precision() -> None:
    source = {
        "id": "bench_one",
        "created_at": "2026-07-31T17:00:00Z",
        "speed_tokens_per_second": 1.234567890123,
    }
    destination = {
        "id": "bench_one",
        "created_at": "2026-07-31 17:00:00.000",
        "speed_tokens_per_second": 1.2345678806304932,
    }
    assert _canonical(source, surface="benchmark") == _canonical(
        destination,
        surface="benchmark",
    )


def test_rollup_parity_ignores_rebuild_time_and_normalizes_null_region() -> None:
    source = {
        "id": "rollup_one",
        "period_start": "2026-07-30T17:00:00Z",
        "last_checked_at": "2026-07-30T17:59:59.999987Z",
        "updated_at": "2026-07-30T18:00:01.123456Z",
        "target_region": None,
    }
    destination = {
        "id": "rollup_one",
        "period_start": "2026-07-30 17:00:00",
        "last_checked_at": "2026-07-30 17:59:59.999",
        "updated_at": "2026-07-31 12:00:00.000",
        "target_region": "",
    }
    assert _canonical(source, surface="rollup") == _canonical(
        destination,
        surface="rollup",
    )


def test_parity_grace_excludes_recent_raw_rows() -> None:
    cutoff = dt.datetime(2026, 7, 31, 17, 0, tzinfo=dt.UTC)
    assert _stable_source_row(
        {"created_at": "2026-07-31T16:59:59Z"},
        surface="synthetic",
        cutoff=cutoff,
    )
    assert not _stable_source_row(
        {"created_at": "2026-07-31T17:00:01Z"},
        surface="synthetic",
        cutoff=cutoff,
    )


def test_parity_grace_excludes_rows_written_recently_even_with_old_event_time() -> None:
    cutoff = dt.datetime(2026, 7, 31, 17, 0, tzinfo=dt.UTC)

    def row(write_time: dt.datetime) -> object:
        cell = SimpleNamespace(
            value=b"{}",
            timestamp_micros=int(write_time.timestamp() * 1_000_000),
        )
        return SimpleNamespace(cells={"benchmark": {b"body": [cell]}})

    assert _stable_source_write(
        row(cutoff - dt.timedelta(seconds=1)),
        families=("benchmark", "m"),
        cutoff=cutoff,
    )
    assert not _stable_source_write(
        row(cutoff + dt.timedelta(seconds=1)),
        families=("benchmark", "m"),
        cutoff=cutoff,
    )


def test_benchmark_parity_applies_grace_to_bigtable_write_time() -> None:
    now = dt.datetime.now(dt.UTC)

    def row(sample_id: str, write_time: dt.datetime) -> object:
        payload = {
            "id": sample_id,
            "model": "test/model",
            "provider": "test",
            "provider_name": "Test",
            "status": "completed",
            "usage_type": "credits",
            "streamed": False,
            "created_at": (now - dt.timedelta(hours=1)).isoformat(),
        }
        cell = SimpleNamespace(
            value=json.dumps(payload).encode("utf-8"),
            timestamp_micros=int(write_time.timestamp() * 1_000_000),
        )
        return SimpleNamespace(cells={"benchmark": {b"body": [cell]}})

    class Table:
        def read_rows(self, **_: object) -> list[object]:
            return [
                row("recent-write", now),
                row("stable-write", now - dt.timedelta(minutes=10)),
            ]

    samples = _source_rows(
        Table(),
        surface="benchmark",
        limit=2,
        grace_seconds=120,
    )

    assert set(samples) == {"stable-write"}


def test_parity_waits_for_shared_heartbeat_bucket_to_close() -> None:
    bucket_start = dt.datetime(2026, 8, 13, 15, 0, tzinfo=dt.UTC)
    bucket = int(bucket_start.timestamp() // 300)
    payload = {
        "id": f"syn_hb_scheduler_remediator_{bucket}",
        "probe_type": "heartbeat",
        "created_at": "2026-08-13T15:00:01Z",
    }

    assert not _stable_source_row(
        payload,
        surface="synthetic",
        cutoff=dt.datetime(2026, 8, 13, 15, 3, tzinfo=dt.UTC),
    )
    assert _stable_source_row(
        payload,
        surface="synthetic",
        cutoff=dt.datetime(2026, 8, 13, 15, 5, tzinfo=dt.UTC),
    )


def test_parity_excludes_incomplete_rollup_periods() -> None:
    cutoff = dt.datetime(2026, 8, 1, 0, 2, tzinfo=dt.UTC)
    assert _stable_source_row(
        {"period": "hour", "period_start": "2026-07-31T23:00:00Z"},
        surface="rollup",
        cutoff=cutoff,
    )
    assert not _stable_source_row(
        {"period": "day", "period_start": "2026-08-01T00:00:00Z"},
        surface="rollup",
        cutoff=cutoff,
    )


def test_rollup_parity_rebuilds_from_bounded_raw_bigtable_samples() -> None:
    created_at = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).replace(
        minute=15,
        second=0,
        microsecond=0,
    )
    sample = SyntheticProbeSample(
        id="syn_raw_one",
        probe_type="tls_health",
        target="canonical",
        target_url="https://api.trustedrouter.com/health",
        monitor_region="us-central1",
        status="up",
        target_region="us-central1",
        latency_milliseconds=20,
        ttfb_milliseconds=19,
        created_at=created_at.isoformat().replace("+00:00", "Z"),
    )
    cell = SimpleNamespace(
        value=json.dumps(dataclasses.asdict(sample)).encode("utf-8")
    )
    row = SimpleNamespace(cells={"synthetic": {b"body": [cell]}})
    duplicate = dataclasses.replace(
        sample,
        created_at=(created_at + dt.timedelta(minutes=2)).isoformat().replace(
            "+00:00", "Z"
        ),
    )
    duplicate_cell = SimpleNamespace(
        value=json.dumps(dataclasses.asdict(duplicate)).encode("utf-8")
    )
    duplicate_row = SimpleNamespace(cells={"synthetic": {b"body": [duplicate_cell]}})

    class Table:
        start_key: bytes = b""
        end_key: bytes = b""

        def read_rows(self, *, start_key: bytes, end_key: bytes, filter_: object) -> list[object]:
            self.start_key = start_key
            self.end_key = end_key
            return [row, duplicate_row]

    table = Table()
    rollups = _source_rollups_from_raw(table, limit=10, grace_seconds=600)
    assert rollups
    assert table.start_key.startswith(b"synthetic_recent#")
    assert table.end_key.startswith(b"synthetic_recent#")
    assert table.start_key < table.end_key
    assert all(payload["sample_count"] == 1 for payload in rollups.values())


def test_synthetic_parity_keeps_newest_duplicate_id() -> None:
    now = dt.datetime.now(dt.UTC)
    bucket = int((now - dt.timedelta(minutes=10)).timestamp() // 300)
    bucket_start = dt.datetime.fromtimestamp(bucket * 300, tz=dt.UTC)

    def row(sample_id: str, created_at: dt.datetime) -> object:
        sample = SyntheticProbeSample(
            id=sample_id,
            probe_type="heartbeat",
            target="scheduler:remediator",
            target_url="",
            monitor_region="us-central1",
            status="up",
            created_at=created_at.isoformat().replace("+00:00", "Z"),
        )
        cell = SimpleNamespace(
            value=json.dumps(dataclasses.asdict(sample)).encode("utf-8")
        )
        return SimpleNamespace(cells={"synthetic": {b"body": [cell]}})

    shared_id = f"syn_hb_shared_{bucket}"
    newest_created_at = bucket_start + dt.timedelta(minutes=3)
    newest = row(shared_id, newest_created_at)
    older_duplicate = row(shared_id, bucket_start + dt.timedelta(minutes=1))
    other = row(
        f"syn_hb_other_{bucket - 1}",
        bucket_start - dt.timedelta(minutes=2),
    )

    class Table:
        def read_rows(self, **_: object) -> list[object]:
            return [newest, older_duplicate, other]

    samples = _source_rows(Table(), surface="synthetic", limit=2)

    assert samples[shared_id]["created_at"] == newest_created_at.isoformat().replace(
        "+00:00", "Z"
    )
