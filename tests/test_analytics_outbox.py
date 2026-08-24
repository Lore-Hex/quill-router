from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any

import pytest

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

    def oldest_commit_ts(self) -> dt.datetime | None:
        return self.rows[0].commit_ts if self.rows else None


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
    def __enter__(self) -> _ReadSnapshot:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def execute_sql(self, *_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
        return []


class _ReadDatabase:
    def __init__(self) -> None:
        self.multi_use_values: list[bool] = []

    def snapshot(self, *, multi_use: bool = False) -> _ReadSnapshot:
        self.multi_use_values.append(multi_use)
        return _ReadSnapshot()


def test_sharded_reads_request_multi_use_spanner_snapshots() -> None:
    database = _ReadDatabase()
    source = object.__new__(SpannerOutboxSource)
    source._database = database
    source._pt = _ParamTypes()
    source._shard_count = 2

    assert source.fetch(limit=10) == []
    assert source.oldest_commit_ts() is None
    assert database.multi_use_values == [True, True]


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
