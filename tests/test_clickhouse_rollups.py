from __future__ import annotations

import datetime as dt

import pytest

from clickhouse.rollup_analytics import (
    RollupPartition,
    _parse_granularities,
    planned_partitions,
    recompute_partition,
)


class FakeExecutor:
    def __init__(self, responses: list[bytes]) -> None:
        self.responses = responses
        self.queries: list[str] = []

    def execute(self, query: str) -> bytes:
        self.queries.append(query)
        return self.responses.pop(0) if self.responses else b""


def test_hourly_planner_excludes_open_hour_and_rebuilds_recent_days() -> None:
    now = dt.datetime(2026, 7, 31, 12, 34, tzinfo=dt.UTC)
    partitions = planned_partitions("hourly", now=now)

    assert [part.partition_id for part in partitions] == ["20260729", "20260730", "20260731"]
    assert partitions[-1].end == dt.datetime(2026, 7, 31, 12, tzinfo=dt.UTC)


def test_daily_planner_rebuilds_previous_and_current_month_but_not_today() -> None:
    now = dt.datetime(2026, 7, 2, 12, 34, tzinfo=dt.UTC)
    partitions = planned_partitions("daily", now=now)

    assert [part.partition_id for part in partitions] == ["202606", "202607"]
    assert partitions[-1].end == dt.datetime(2026, 7, 2, tzinfo=dt.UTC)


def test_monthly_planner_only_publishes_complete_months() -> None:
    now = dt.datetime(2026, 7, 2, 12, 34, tzinfo=dt.UTC)
    partitions = planned_partitions("monthly", now=now)

    assert [part.partition_id for part in partitions] == ["202606"]
    assert partitions[0].start == dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    assert partitions[0].end == dt.datetime(2026, 7, 1, tzinfo=dt.UTC)


def test_backfill_planner_covers_every_historical_partition() -> None:
    now = dt.datetime(2026, 7, 31, 12, 34, tzinfo=dt.UTC)
    daily = planned_partitions(
        "daily",
        now=now,
        backfill_start=dt.date(2025, 11, 18),
    )
    monthly = planned_partitions(
        "monthly",
        now=now,
        backfill_start=dt.date(2025, 11, 18),
    )

    assert daily[0].partition_id == "202511"
    assert daily[-1].partition_id == "202607"
    assert monthly[0].partition_id == "202511"
    assert monthly[-1].partition_id == "202606"


def test_recompute_verifies_staging_before_atomic_replace_and_live_after() -> None:
    executor = FakeExecutor([b"17\n", b"", b"", b"17\n", b"", b"17\n"])
    partition = RollupPartition(
        "hourly",
        dt.datetime(2026, 7, 30, tzinfo=dt.UTC),
        dt.datetime(2026, 7, 31, tzinfo=dt.UTC),
        "20260730",
    )

    assert recompute_partition(executor, partition) == 17
    assert executor.queries[1] == "TRUNCATE TABLE provider_analytics_hourly_staging"
    assert executor.queries[2].startswith("INSERT INTO provider_analytics_hourly_staging SELECT")
    assert "REPLACE PARTITION ID '20260730'" in executor.queries[4]
    assert "FROM provider_analytics_hourly_staging" in executor.queries[4]


def test_recompute_does_not_publish_a_parity_mismatch() -> None:
    executor = FakeExecutor([b"17\n", b"", b"", b"16\n"])
    partition = RollupPartition(
        "daily",
        dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
        dt.datetime(2026, 7, 31, tzinfo=dt.UTC),
        "202607",
    )

    with pytest.raises(RuntimeError, match="rollup parity mismatch"):
        recompute_partition(executor, partition)
    assert all("REPLACE PARTITION" not in query for query in executor.queries)


def test_empty_source_removes_stale_partition_without_insert() -> None:
    executor = FakeExecutor([b"0\n", b""])
    partition = RollupPartition(
        "monthly",
        dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
        "202606",
    )

    assert recompute_partition(executor, partition) == 0
    assert executor.queries[-1] == "ALTER TABLE provider_analytics_monthly DROP PARTITION ID '202606'"


def test_granularity_parser_is_fail_closed() -> None:
    assert tuple(_parse_granularities("all")) == ("hourly", "daily", "monthly")
    with pytest.raises(Exception, match="granularity"):
        _parse_granularities("weekly")
