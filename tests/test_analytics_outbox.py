from __future__ import annotations

import dataclasses
import datetime as dt
import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from clickhouse import ingest_outbox
from clickhouse.backfill_benchmark_samples import normalise
from clickhouse.ingest_outbox import (
    DrainMetrics,
    OutboxRow,
    SpannerOutboxSource,
    drain_once,
    normalise_outbox_payload,
)
from clickhouse.reconcile_benchmark_samples import (
    CLICKHOUSE_COLUMNS,
    _add_row,
    _reverse_time_key,
)
from trusted_router.config import Settings
from trusted_router.storage_gcp_analytics_outbox import (
    SpannerAnalyticsOutbox,
    analytics_outbox_shard,
)
from trusted_router.storage_gcp_generations import SpannerGenerations
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.types import UsageType


def _sample() -> ProviderBenchmarkSample:
    return ProviderBenchmarkSample(
        id="bench-0123456789abcdef0123456789abcdef",
        model="anthropic/claude-haiku-4.5",
        provider="anthropic",
        provider_name="Anthropic",
        status="error",
        usage_type=UsageType.CREDITS,
        streamed=True,
        workspace_id="ws-analytics-test",
        input_tokens=12,
        output_tokens=3,
        speed_tokens_per_second=7.125,
        elapsed_milliseconds=210,
        first_token_milliseconds=90,
        error_status=429,
        error_type="rate_limit",
        created_at="2026-07-28T12:34:56.789Z",
    )


class _ParamTypes:
    INT64 = "INT64"
    STRING = "STRING"


class _Transaction:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []

    def execute_update(
        self,
        sql: str,
        *,
        params: dict[str, Any],
        param_types: dict[str, Any],
    ) -> None:
        self.calls.append((sql, params, param_types))


class _Database:
    def __init__(self) -> None:
        self.transaction = _Transaction()

    def run_in_transaction(self, callback: Any) -> None:
        callback(self.transaction)


def test_analytics_outbox_is_disabled_by_default() -> None:
    assert Settings().analytics_outbox_enabled is False


def test_enqueue_uses_commit_timestamp_and_deterministic_shard() -> None:
    database = _Database()
    sample = _sample()
    SpannerAnalyticsOutbox(database, _ParamTypes()).enqueue(sample)

    [(sql, params, param_types)] = database.transaction.calls
    assert "PENDING_COMMIT_TIMESTAMP()" in sql
    assert params["event_id"] == sample.id
    assert params["shard"] == analytics_outbox_shard(sample.id)
    assert json.loads(params["payload"])["error_status"] == 429
    assert param_types == {
        "shard": "INT64",
        "event_id": "STRING",
        "payload": "STRING",
    }


def test_bigtable_and_outbox_best_effort_attempts_are_independent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts: list[str] = []

    def fail_bigtable(*_args: Any) -> None:
        attempts.append("bigtable")
        raise RuntimeError("Bigtable unavailable")

    class FailingOutbox:
        def enqueue(self, _sample: ProviderBenchmarkSample) -> None:
            attempts.append("outbox")
            raise RuntimeError("Spanner unavailable")

    monkeypatch.setattr(
        "trusted_router.storage_gcp_generations._bt_write_provider_benchmark",
        fail_bigtable,
    )
    generations = object.__new__(SpannerGenerations)
    generations._bt_table = object()
    generations._benchmark_family = "benchmark"
    generations._analytics_outbox = FailingOutbox()

    # Both failures are analytics-only and may not escape to settle callers.
    generations.record_benchmark(_sample())
    # The durable Spanner hand-off is authoritative. The migration-only
    # Bigtable mirror is attempted afterward and cannot prevent the enqueue.
    assert attempts == ["outbox", "bigtable"]


def test_normalise_is_identical_for_backfill_and_outbox_payload() -> None:
    raw = dataclasses.asdict(_sample())
    # Exercise the coercions that historically diverged between ingestion
    # paths: string status codes and nullable numeric strings.
    raw["error_status"] = "429"
    raw["elapsed_milliseconds"] = "210"
    raw["speed_tokens_per_second"] = "7.125"
    expected = normalise(raw)

    assert normalise_outbox_payload(json.dumps(raw)) == expected
    assert expected is not None
    assert expected["error_status"] == 429
    assert expected["elapsed_milliseconds"] == 210
    assert expected["workspace_id"] == "ws-analytics-test"
    assert "workspace_id" in CLICKHOUSE_COLUMNS


class _Source:
    def __init__(self, rows: list[OutboxRow], *, fail_delete_once: bool = False) -> None:
        self.rows = rows
        self.deleted: list[OutboxRow] = []
        self.fail_delete_once = fail_delete_once

    def fetch(self, *, limit: int) -> list[OutboxRow]:
        return self.rows[:limit]

    def delete(self, rows: list[OutboxRow]) -> None:
        if self.fail_delete_once:
            self.fail_delete_once = False
            raise RuntimeError("Spanner delete unavailable")
        self.deleted.extend(rows)
        deleted = set(rows)
        self.rows = [row for row in self.rows if row not in deleted]

    def reset_scan_floors(self) -> None:
        pass


class _Writer:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.batches: list[list[dict[str, Any]]] = []

    def insert(self, rows: list[dict[str, Any]]) -> None:
        self.batches.append(rows)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("ClickHouse unavailable")


def _outbox_row() -> OutboxRow:
    return OutboxRow(
        shard=3,
        commit_ts=dt.datetime(2026, 7, 28, 12, 35, tzinfo=dt.UTC),
        event_id=_sample().id,
        payload=json.dumps(dataclasses.asdict(_sample())),
    )


def test_cursor_does_not_advance_until_clickhouse_ack_and_retry_succeeds() -> None:
    row = _outbox_row()
    source = _Source([row])
    writer = _Writer(failures=1)
    metrics = DrainMetrics()

    with pytest.raises(RuntimeError, match="ClickHouse unavailable"):
        drain_once(source, writer, metrics, batch_size=100)
    assert source.rows == [row]
    assert source.deleted == []
    assert metrics.clickhouse_insert_errors_total == 1

    result = drain_once(source, writer, metrics, batch_size=100)
    assert result.inserted == 1
    assert source.rows == []
    assert source.deleted == [row]
    assert metrics.rows_ingested_total == 1


def test_delete_failure_replays_acknowledged_batch_without_losing_cursor() -> None:
    row = _outbox_row()
    source = _Source([row], fail_delete_once=True)
    writer = _Writer()
    metrics = DrainMetrics()

    with pytest.raises(RuntimeError, match="Spanner delete unavailable"):
        drain_once(source, writer, metrics, batch_size=100)
    assert source.rows == [row]
    assert len(writer.batches) == 1
    assert metrics.clickhouse_insert_errors_total == 0

    drain_once(source, writer, metrics, batch_size=100)
    assert source.rows == []
    assert len(writer.batches) == 2


class _ReadSnapshot:
    def __init__(self, database: _ReadDatabase) -> None:
        self.database = database

    def __enter__(self) -> _ReadSnapshot:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute_sql(
        self, sql: str, *, params: dict[str, Any], param_types: dict[str, Any]
    ) -> list[tuple[Any, ...]]:
        self.database.calls.append((sql, params, param_types))
        if self.database.fail_fetch:
            raise RuntimeError("fetch failed")
        rows = [row for row in self.database.rows if row.shard == params["shard"]]
        if "floor" in params:
            assert "AND commit_ts >= @floor" in sql
            rows = [row for row in rows if row.commit_ts >= params["floor"]]
        else:
            assert "@floor" not in sql
        rows.sort(key=lambda row: (row.commit_ts, row.event_id))
        return [(*row.key, row.payload) for row in rows[:params["limit"]]]


class _DeleteBatch:
    def __init__(self, database: _ReadDatabase) -> None:
        self.database = database
        self.keys: list[list[Any]] = []

    def __enter__(self) -> _DeleteBatch:
        return self

    def delete(self, table: str, keyset: Any) -> None:
        assert table == "tr_analytics_outbox"
        self.keys = keyset.keys

    def __exit__(self, *_args: Any) -> None:
        if self.database.fail_delete:
            raise RuntimeError("delete commit failed")
        self.database.rows = [
            row for row in self.database.rows if list(row.key) not in self.keys
        ]


class _ReadDatabase:
    def __init__(self) -> None:
        self.multi_use_values: list[bool] = []
        self.rows: list[OutboxRow] = []
        self.calls: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
        self.fail_fetch = False
        self.fail_delete = False

    def snapshot(self, *, multi_use: bool = False) -> _ReadSnapshot:
        self.multi_use_values.append(multi_use)
        return _ReadSnapshot(self)

    def batch(self) -> _DeleteBatch:
        return _DeleteBatch(self)


@pytest.fixture
def spanner_source(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_ReadDatabase, Callable[[], SpannerOutboxSource]]:
    from google.cloud import spanner

    database = _ReadDatabase()
    client = SimpleNamespace(instance=lambda _name: SimpleNamespace(database=lambda _db: database))
    monkeypatch.setattr(spanner, "Client", lambda **_kwargs: client)

    def create() -> SpannerOutboxSource:
        return SpannerOutboxSource(project="fake", instance="fake", database="fake", shard_count=4)

    return database, create


def test_sharded_reads_request_multi_use_spanner_snapshots(
    spanner_source: tuple[_ReadDatabase, Callable[[], SpannerOutboxSource]],
) -> None:
    database, create = spanner_source
    assert create().fetch(limit=10) == []
    assert database.multi_use_values == [True]
    assert len(database.calls) == 4


def test_floor_preserves_timestamp_ties_and_truncated_batches(
    spanner_source: tuple[_ReadDatabase, Callable[[], SpannerOutboxSource]],
) -> None:
    from google.cloud.spanner_v1 import param_types

    database, create = spanner_source
    source = create()
    first = dataclasses.replace(_outbox_row(), event_id="a")
    tied = dataclasses.replace(first, event_id="b")
    later = dataclasses.replace(first, event_id="c", commit_ts=first.commit_ts + dt.timedelta(seconds=1))
    other_shard = dataclasses.replace(later, shard=0)
    database.rows = [later, tied, first, other_shard]
    writer = _Writer()
    metrics = DrainMetrics()

    result = drain_once(source, writer, metrics, batch_size=1)
    assert result.oldest_commit_ts == first.commit_ts
    assert database.rows == [later, tied, other_shard]
    assert all("floor" not in params for _, params, _ in database.calls)
    assert source._floors == {3: first.commit_ts}

    database.calls.clear()
    assert source.fetch(limit=10) == [tied, other_shard, later]
    for sql, params, types in database.calls:
        if params["shard"] == 3:
            assert "AND commit_ts >= @floor" in sql
            assert params["floor"] == first.commit_ts
            assert types["floor"] == param_types.TIMESTAMP
        else:
            assert "floor" not in params
    # Reading ahead must not advance the floor past unacknowledged rows.
    assert source._floors == {3: first.commit_ts}
    drain_once(source, writer, metrics, batch_size=10)
    assert database.rows == []
    assert source._floors == {0: later.commit_ts, 3: later.commit_ts}

    # Even a subsequently visible timestamp tie remains eligible (inclusive).
    database.rows = [dataclasses.replace(later, event_id="late-tie")]
    assert source.fetch(limit=10) == database.rows


@pytest.mark.parametrize("failure", ["fetch", "payload", "insert", "delete"])
def test_drain_error_clears_every_floor_and_replays(
    spanner_source: tuple[_ReadDatabase, Callable[[], SpannerOutboxSource]],
    failure: str,
) -> None:
    database, create = spanner_source
    source = create()
    row = _outbox_row()
    database.rows = [row, dataclasses.replace(row, shard=0)]
    metrics = DrainMetrics()
    drain_once(source, _Writer(), metrics, batch_size=10)
    assert set(source._floors) == {0, 3}

    pending = dataclasses.replace(row, commit_ts=row.commit_ts + dt.timedelta(seconds=1))
    database.rows = [pending]
    database.fail_fetch = failure == "fetch"
    database.fail_delete = failure == "delete"
    if failure == "payload":
        database.rows = [dataclasses.replace(pending, payload="null")]
    writer = _Writer(failures=int(failure == "insert"))
    with pytest.raises((RuntimeError, ValueError)):
        drain_once(source, writer, metrics, batch_size=10)
    assert source._floors == {}
    assert len(database.rows) == 1

    database.fail_fetch = database.fail_delete = False
    # An older row is deliberately injected to prove the retry is a full scan.
    database.rows = [dataclasses.replace(row, commit_ts=row.commit_ts - dt.timedelta(seconds=1)), pending]
    database.calls.clear()
    drain_once(source, writer, metrics, batch_size=10)
    assert all("floor" not in params for _, params, _ in database.calls)
    assert database.rows == []


def test_restart_restores_full_scan(
    spanner_source: tuple[_ReadDatabase, Callable[[], SpannerOutboxSource]],
) -> None:
    database, create = spanner_source
    source = create()
    row = _outbox_row()
    database.rows = [row]
    drain_once(source, _Writer(), DrainMetrics(), batch_size=10)
    assert source._floors
    restarted = create()
    database.rows = [dataclasses.replace(row, commit_ts=row.commit_ts - dt.timedelta(days=1))]
    database.calls.clear()
    assert restarted.fetch(limit=10) == database.rows
    assert all("floor" not in params for _, params, _ in database.calls)


@pytest.mark.parametrize("busy", [False, True])
def test_main_pass_executes_one_statement_per_shard_even_when_logging(
    monkeypatch: pytest.MonkeyPatch,
    spanner_source: tuple[_ReadDatabase, Callable[[], SpannerOutboxSource]],
    busy: bool,
) -> None:
    database, _ = spanner_source
    database.rows = [_outbox_row()] if busy else []
    monkeypatch.setattr("sys.argv", ["ingest_outbox", "--once", "--shards", "4", "--metrics-seconds", "0"])
    monkeypatch.setenv("CH_PASSWORD", "fake")
    monkeypatch.delenv("TR_OUTBOX_IDLE_MAX_SECONDS", raising=False)
    monkeypatch.setattr(ingest_outbox, "ClickHouseWriter", lambda **_kwargs: _Writer())
    assert ingest_outbox.main() == 0
    assert len(database.calls) == 4
    assert database.rows == []


@pytest.mark.parametrize("cap", [None, "2"])
@pytest.mark.parametrize("insert_failure", [False, True])
def test_idle_backoff_progression_cap_and_reset(
    monkeypatch: pytest.MonkeyPatch,
    spanner_source: tuple[_ReadDatabase, Callable[[], SpannerOutboxSource]],
    cap: str | None,
    insert_failure: bool,
) -> None:
    database, _ = spanner_source
    monkeypatch.setattr("sys.argv", ["ingest_outbox", "--shards", "4", "--poll-seconds", "0.5"])
    monkeypatch.setenv("CH_PASSWORD", "fake")
    if cap is None:
        monkeypatch.delenv("TR_OUTBOX_IDLE_MAX_SECONDS", raising=False)
    else:
        monkeypatch.setenv("TR_OUTBOX_IDLE_MAX_SECONDS", cap)
    writer = _Writer(failures=int(insert_failure))
    monkeypatch.setattr(ingest_outbox, "ClickHouseWriter", lambda **_kwargs: writer)
    sleeps: list[float] = []

    class StopLoop(Exception):
        pass

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 6:
            database.rows = [_outbox_row()]
        if len(sleeps) == 8:
            raise StopLoop

    monkeypatch.setattr(ingest_outbox.time, "sleep", sleep)
    with pytest.raises(StopLoop):
        ingest_outbox.main()
    expected = [0.5, 1, 2, 4, 5, 5] if cap is None else [0.5, 1, 2, 2, 2, 2]
    # A failed insert resets immediately, then the successful retry resets again.
    assert sleeps == expected + [0.5, 0.5 if insert_failure else 1]
    assert len(writer.batches) == 1 + int(insert_failure)
    assert len(database.calls) == 9 * 4  # Nine passes, including the failed pass if any.


@pytest.mark.parametrize("poll, cap", [("nan", "5"), ("1", "inf"), ("1", "0.5")])
def test_invalid_idle_intervals_fail_before_opening_spanner(
    monkeypatch: pytest.MonkeyPatch, poll: str, cap: str,
) -> None:
    monkeypatch.setattr("sys.argv", ["ingest_outbox", "--poll-seconds", poll])
    monkeypatch.setenv("TR_OUTBOX_IDLE_MAX_SECONDS", cap)
    with pytest.raises(SystemExit) as exc:
        ingest_outbox.main()
    assert exc.value.code == 2


def test_reconciler_reverse_range_sorts_newer_events_first() -> None:
    older = dt.datetime(2026, 7, 20, tzinfo=dt.UTC)
    newer = dt.datetime(2026, 7, 21, tzinfo=dt.UTC)
    assert _reverse_time_key(newer) < _reverse_time_key(older)


def test_reconciler_uses_a_closed_wall_clock_window() -> None:
    target: dict[str, dict[str, str]] = {}
    raw = dataclasses.asdict(_sample())
    lower = dt.datetime(2026, 7, 28, 12, tzinfo=dt.UTC)
    upper = dt.datetime(2026, 7, 28, 13, tzinfo=dt.UTC)

    _add_row(target, raw, cutoff=lower, upper=upper)
    assert set(target["2026-07-28"]) == {_sample().id}

    raw["created_at"] = upper.isoformat()
    _add_row(target, raw, cutoff=lower, upper=upper)
    assert len(target["2026-07-28"]) == 1
