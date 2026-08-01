from __future__ import annotations

import dataclasses
import datetime as dt
import json
from types import SimpleNamespace

from clickhouse.verify_operational_parity import (
    _canonical,
    _source_rollups_from_raw,
    _stable_source_row,
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

    class Table:
        start_key: bytes = b""
        end_key: bytes = b""

        def read_rows(self, *, start_key: bytes, end_key: bytes, filter_: object) -> list[object]:
            self.start_key = start_key
            self.end_key = end_key
            return [row]

    table = Table()
    rollups = _source_rollups_from_raw(table, limit=10, grace_seconds=600)
    assert rollups
    assert table.start_key.startswith(b"synthetic_recent#")
    assert table.end_key.startswith(b"synthetic_recent#")
    assert table.start_key < table.end_key
    assert all(payload["sample_count"] == 1 for payload in rollups.values())
