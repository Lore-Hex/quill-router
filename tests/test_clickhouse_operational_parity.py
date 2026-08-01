from __future__ import annotations

import datetime as dt

from clickhouse.verify_operational_parity import _canonical, _stable_source_row


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
