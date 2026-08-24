from __future__ import annotations

import dataclasses
import datetime as dt
import json
from types import SimpleNamespace

import pytest

from clickhouse.ingest_operational_outbox import CanonicalOperationalEvent
from clickhouse.reconcile_synthetic_samples import reconcile, source_samples
from trusted_router.storage_models import SyntheticProbeSample


def _sample(sample_id: str, created_at: dt.datetime) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=sample_id,
        probe_type="remediation",
        target="route-quarantine:test/model",
        target_url="",
        monitor_region="us-central1",
        status="down",
        error_type="would quarantine",
        created_at=created_at.isoformat().replace("+00:00", "Z"),
    )


def _row(sample: SyntheticProbeSample, *, written_at: dt.datetime) -> object:
    cell = SimpleNamespace(
        value=json.dumps(dataclasses.asdict(sample)).encode(),
        timestamp_micros=int(written_at.timestamp() * 1_000_000),
    )
    return SimpleNamespace(cells={"synthetic": {b"body": [cell]}})


class _Table:
    def __init__(self, rows_by_day: dict[str, list[object]]) -> None:
        self.rows_by_day = rows_by_day
        self.limits: list[int] = []

    def read_rows(
        self,
        *,
        start_key: bytes,
        end_key: bytes,
        limit: int,
        filter_: object,
    ) -> list[object]:
        del end_key, filter_
        self.limits.append(limit)
        day = start_key.decode().split("#")[1]
        return self.rows_by_day.get(day, [])[:limit]


class _ClickHouse:
    def __init__(self, ids: set[str]) -> None:
        self.ids = ids
        self.queries: list[str] = []

    def query(
        self,
        sql: str,
        *,
        input_bytes: bytes | None = None,
        external_ids: bool = False,
    ) -> str:
        assert external_ids
        assert input_bytes is not None
        self.queries.append(sql)
        wanted = set(input_bytes.decode().splitlines())
        return "".join(f"{sample_id}\n" for sample_id in sorted(wanted & self.ids))


class _Writer:
    def __init__(self, clickhouse: _ClickHouse) -> None:
        self.clickhouse = clickhouse
        self.batches: list[list[CanonicalOperationalEvent]] = []

    def insert(self, events: list[CanonicalOperationalEvent]) -> None:
        self.batches.append(events)
        self.clickhouse.ids.update(str(event.row["id"]) for event in events)


def test_source_samples_reads_complete_recent_day_indexes_and_applies_grace() -> None:
    now = dt.datetime(2026, 8, 22, 12, tzinfo=dt.UTC)
    stable = _sample("syn_stable", now - dt.timedelta(hours=2))
    recent = _sample("syn_recent", now - dt.timedelta(minutes=1))
    table = _Table(
        {
            "2026-08-22": [
                _row(stable, written_at=now - dt.timedelta(hours=1)),
                _row(recent, written_at=now - dt.timedelta(minutes=1)),
            ],
            "2026-08-21": [],
            "2026-08-20": [],
        }
    )

    rows = source_samples(
        table,
        now=now,
        days=3,
        per_day_limit=100,
        grace_seconds=600,
    )

    assert set(rows) == {"syn_stable"}
    assert table.limits == [101, 101, 101]


def test_source_samples_fails_closed_when_a_day_exceeds_the_safety_limit() -> None:
    now = dt.datetime(2026, 8, 22, 12, tzinfo=dt.UTC)
    rows = [
        _row(
            _sample(f"syn_{index}", now - dt.timedelta(hours=2)),
            written_at=now - dt.timedelta(hours=1),
        )
        for index in range(3)
    ]
    table = _Table({"2026-08-22": rows})

    with pytest.raises(RuntimeError, match="exceeded the 2 row safety limit"):
        source_samples(
            table,
            now=now,
            days=1,
            per_day_limit=2,
            grace_seconds=600,
        )


def test_reconcile_inserts_only_missing_rows_and_verifies_the_repair() -> None:
    now = dt.datetime(2026, 8, 22, 12, tzinfo=dt.UTC)
    source = {
        "syn_present": {"id": "syn_present", "created_at": now.isoformat()},
        "syn_missing": {"id": "syn_missing", "created_at": now.isoformat()},
    }
    clickhouse = _ClickHouse({"syn_present"})
    writer = _Writer(clickhouse)

    result = reconcile(
        source=source,
        clickhouse=clickhouse,
        writer=writer,
        apply=True,
        batch_size=10,
        now=now,
    )

    assert result.ok
    assert result.missing_before == 1
    assert result.repaired == 1
    assert result.unresolved == 0
    assert len(writer.batches) == 1
    [event] = writer.batches[0]
    assert event.event_kind == "synthetic"
    assert event.row["id"] == "syn_missing"
    assert event.row["ingest_version"] == now.isoformat()


def test_reconcile_dry_run_reports_missing_without_writing() -> None:
    now = dt.datetime(2026, 8, 22, 12, tzinfo=dt.UTC)
    source = {"syn_missing": {"id": "syn_missing"}}
    clickhouse = _ClickHouse(set())
    writer = _Writer(clickhouse)

    result = reconcile(
        source=source,
        clickhouse=clickhouse,
        writer=writer,
        apply=False,
        batch_size=10,
        now=now,
    )

    assert not result.ok
    assert result.missing_before == 1
    assert result.repaired == 0
    assert result.unresolved == 1
    assert writer.batches == []


def test_reconcile_rejects_invalid_source_ids_before_querying() -> None:
    now = dt.datetime(2026, 8, 22, 12, tzinfo=dt.UTC)
    clickhouse = _ClickHouse(set())

    with pytest.raises(ValueError, match="invalid record ID"):
        reconcile(
            source={"bad id": {"id": "bad id"}},
            clickhouse=clickhouse,
            writer=_Writer(clickhouse),
            apply=True,
            batch_size=10,
            now=now,
        )
