from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from clickhouse.rollup_reservation_overruns import (
    PROJECT,
    SPANNER_DATABASE,
    SPANNER_INSTANCE,
    OverrunAggregate,
    _parse_args,
    aggregate_reservation_overruns,
    build_clickhouse_rows,
    rollup_reservation_overruns,
)

ROOT = Path(__file__).parents[1]


class FakeReservationSource:
    def __init__(self, rows: list[Mapping[str, Any]]) -> None:
        self.rows = rows
        self.windows: list[tuple[dt.datetime, dt.datetime]] = []

    def fetch(
        self,
        *,
        window_start: dt.datetime,
        window_end: dt.datetime,
    ) -> list[Mapping[str, Any]]:
        self.windows.append((window_start, window_end))
        return self.rows


class FakeWriter:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def upsert(self, rows: list[dict[str, Any]]) -> None:
        self.rows.extend(rows)


def test_rollup_defaults_to_the_authoritative_production_spanner_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("GCP_PROJECT_ID", "SPANNER_INSTANCE_ID", "SPANNER_DATABASE_ID"):
        monkeypatch.delenv(name, raising=False)
    args = _parse_args([])

    assert PROJECT == args.project == "quill-cloud-proxy"
    assert SPANNER_INSTANCE == args.spanner_instance == "trusted-router-nam6"
    assert SPANNER_DATABASE == args.spanner_database == "trusted-router"


def test_aggregation_counts_only_positive_overruns_by_hour_and_hold_type() -> None:
    rows: list[Mapping[str, Any]] = [
        {
            "terminal_at": "2026-08-25T10:01:00Z",
            "hold_usage_type": "Credits",
            "actual_micro": "100",
            "credit_reserved_micro": "75",
            "settled": True,
        },
        {
            "terminal_at": "2026-08-25T10:59:59+00:00",
            "hold_usage_type": "Credits",
            "actual_micro": "130",
            "credit_reserved_micro": "100",
            "settled": True,
        },
        {
            "terminal_at": dt.datetime(2026, 8, 25, 10, 30, tzinfo=dt.UTC),
            "hold_usage_type": "Credits",
            "actual_micro": 80,
            "credit_reserved_micro": 100,
            "settled": True,
        },
        {
            "terminal_at": "2026-08-25T10:15:00Z",
            "hold_usage_type": "BYOK",
            "actual_micro": 0,
            "credit_reserved_micro": 0,
            "settled": True,
        },
        {
            "terminal_at": "2026-08-25T11:01:00Z",
            "hold_usage_type": "Credits",
            "actual_micro": 500,
            "credit_reserved_micro": 125,
            "settled": True,
        },
        {
            "terminal_at": "2026-08-25T10:20:00Z",
            "hold_usage_type": "Credits",
            "actual_micro": 1_000,
            "credit_reserved_micro": 1,
            "settled": False,
        },
        {
            "terminal_at": None,
            "hold_usage_type": "Credits",
            "actual_micro": 1_000,
            "credit_reserved_micro": 1,
            "settled": True,
        },
    ]

    aggregates = aggregate_reservation_overruns(rows)

    assert aggregates == {
        (dt.datetime(2026, 8, 25, 10, tzinfo=dt.UTC), "BYOK"): OverrunAggregate(
            settled_n=1,
            overrun_n=0,
            overrun_micro=0,
            max_single_overrun_micro=0,
        ),
        (dt.datetime(2026, 8, 25, 10, tzinfo=dt.UTC), "Credits"): OverrunAggregate(
            settled_n=3,
            overrun_n=2,
            overrun_micro=55,
            max_single_overrun_micro=30,
        ),
        (dt.datetime(2026, 8, 25, 11, tzinfo=dt.UTC), "Credits"): OverrunAggregate(
            settled_n=1,
            overrun_n=1,
            overrun_micro=375,
            max_single_overrun_micro=375,
        ),
    }


def test_row_construction_is_sorted_and_versions_every_metric() -> None:
    aggregates = {
        (dt.datetime(2026, 8, 25, 11, tzinfo=dt.UTC), "RegionalCredits"): OverrunAggregate(
            settled_n=9,
            overrun_n=2,
            overrun_micro=450,
            max_single_overrun_micro=300,
        ),
        (dt.datetime(2026, 8, 25, 10, tzinfo=dt.UTC), "Credits"): OverrunAggregate(
            settled_n=7,
            overrun_n=1,
            overrun_micro=25,
            max_single_overrun_micro=25,
        ),
    }

    rows = build_clickhouse_rows(
        aggregates,
        refreshed_at=dt.datetime(2026, 8, 25, 12, 5, 6, 987, tzinfo=dt.UTC),
    )

    assert rows == [
        {
            "hour": "2026-08-25 10:00:00",
            "hold_usage_type": "Credits",
            "settled_n": 7,
            "overrun_n": 1,
            "overrun_micro": 25,
            "max_single_overrun_micro": 25,
            "refreshed_at": "2026-08-25 12:05:06",
        },
        {
            "hour": "2026-08-25 11:00:00",
            "hold_usage_type": "RegionalCredits",
            "settled_n": 9,
            "overrun_n": 2,
            "overrun_micro": 450,
            "max_single_overrun_micro": 300,
            "refreshed_at": "2026-08-25 12:05:06",
        },
    ]


def test_rollup_reads_two_closed_utc_hours_and_dry_run_does_not_write() -> None:
    source = FakeReservationSource([])
    writer = FakeWriter()
    started_at = dt.datetime(2026, 8, 25, 12, 47, 31, tzinfo=dt.UTC)

    result = rollup_reservation_overruns(
        source,
        writer,
        dry_run=True,
        started_at=started_at,
    )

    assert source.windows == [
        (
            dt.datetime(2026, 8, 25, 10, tzinfo=dt.UTC),
            dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC),
        )
    ]
    assert result.window_start == source.windows[0][0]
    assert result.window_end == source.windows[0][1]
    assert writer.rows == []


def test_overrun_schemas_and_hourly_timer_are_installed() -> None:
    replicated = (ROOT / "clickhouse/014_reservation_overruns.sql").read_text()
    single = (ROOT / "clickhouse/015_reservation_overruns_single_node.sql").read_text()
    service = (ROOT / "clickhouse/tr-clickhouse-overrun-rollup.service").read_text()
    timer = (ROOT / "clickhouse/tr-clickhouse-overrun-rollup.timer").read_text()
    installer = (ROOT / "scripts/deploy/clickhouse_live_ingestion.sh").read_text()

    for column in (
        "hour",
        "hold_usage_type",
        "settled_n",
        "overrun_n",
        "overrun_micro",
        "max_single_overrun_micro",
        "refreshed_at",
    ):
        assert column in replicated
        assert column in single
    assert "ON CLUSTER trustedrouter" in replicated
    assert "ReplicatedReplacingMergeTree" in replicated
    assert "ENGINE = ReplacingMergeTree(refreshed_at)" in single
    assert "ORDER BY (hour, hold_usage_type)" in replicated
    assert "ORDER BY (hour, hold_usage_type)" in single

    assert "python -m clickhouse.rollup_reservation_overruns" in service
    assert "User=tr-clickhouse-ingest" in service
    assert "EnvironmentFile=/etc/tr-clickhouse-ingest.env" in service
    assert "ProtectSystem=strict" in service
    assert "OnCalendar=hourly" in timer
    assert "Persistent=true" in timer

    for unit in (
        "tr-clickhouse-overrun-rollup.service",
        "tr-clickhouse-overrun-rollup.timer",
    ):
        assert f"/opt/tr-clickhouse/clickhouse/{unit}" in installer
        assert f"/etc/systemd/system/{unit}" in installer
    assert "014_reservation_overruns.sql" in installer
    assert "systemctl enable --now tr-clickhouse-overrun-rollup.timer" in installer
