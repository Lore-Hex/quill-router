from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from clickhouse.ingest_operational_outbox import (
    ACTIVITY_COLUMNS,
    ACTIVITY_OPTIONAL_DEFAULTS,
    CLIENT_COUNTER_COLUMNS,
    CLIENT_REQUEST_COLUMNS,
    CanonicalOperationalEvent,
    ClickHouseOperationalWriter,
    OperationalOutboxRow,
    drain_once,
    expand_client_events_payload,
    normalise_operational_event,
)
from clickhouse.ingest_operational_outbox_postgres import drain_shard_once
from tests.test_operational_analytics import _generation
from trusted_router.storage_gcp_operational_analytics_outbox import activity_payload

ROOT = Path(__file__).parents[1]
COMMIT_TS = dt.datetime(2026, 8, 17, 12, 1, 2, 345678, tzinfo=dt.UTC)


def _attempt(index: int, **updates: Any) -> dict[str, Any]:
    value = {
        "index": index,
        "host": "apex",
        "outcome": "ok",
        "http_status": 200,
        "error_class": None,
        "error_source": None,
        "should_retry": None,
        "retry_after_ms": None,
        "elapsed_ms": 123,
        "ttfb_ms": 50,
        "request_id": None,
        "moved": False,
    }
    value.update(updates)
    return value


def _event(attempts: list[dict[str, Any]], **updates: Any) -> dict[str, Any]:
    value = {
        "created_at": "2026-08-17T12:00:01.000Z",
        "plane": "inference",
        "endpoint": "responses",
        "method": "POST",
        "streaming": True,
        "provider_pinned": False,
        "model": None,
        "attempts": attempts,
        "final_outcome": attempts[-1]["outcome"],
        "final_http_status": attempts[-1]["http_status"],
        "total_ms": 456,
        "ttft_ms": None,
        "failover_used": len(attempts) > 1,
        "timeout_phase": "none",
        "configured_timeout_ms": None,
        "sample_rate": 1.0,
        "sample_reason": "failure",
        "tr_fault": 0,
        "methodology_version": 1,
    }
    value.update(updates)
    return value


def _counter(**updates: Any) -> dict[str, Any]:
    value = {
        "bucket_start": "2026-08-17T12:00:00Z",
        "level": "request",
        "endpoint": "responses",
        "streaming": True,
        "host": "apex",
        "outcome": "ok",
        "error_class": None,
        "http_status_class": "2xx",
        "timeout_phase": "none",
        "timeout_floor_met": True,
        "provider_pinned": False,
        "requests": 1,
        "attempts": 1,
        "failover_used": 0,
        "first_attempt_success": 1,
        "total_ms_hist": {"lt800": 1},
        "first_event_ms_hist": {},
        "tr_fault": 0,
        "methodology_version": 1,
    }
    value.update(updates)
    return value


def _client_payload() -> dict[str, Any]:
    attempts = [
        _attempt(
            0,
            outcome="transport_error",
            http_status=None,
            error_class="connect_timeout",
            should_retry=True,
            retry_after_ms=None,
            ttfb_ms=None,
            moved=True,
        ),
        _attempt(
            1,
            host="ally",
            outcome="http_error",
            http_status=503,
            error_source="router",
            should_retry=False,
            request_id="rlog_" + "a" * 32,
        ),
        _attempt(2, host="uptime"),
    ]
    return {
        "schema_version": 1,
        "tenant_id": "a" * 64,
        "key_id": "b" * 64,
        "received_at": "2026-08-17T12:01:00.000Z",
        "clock_skew_ms": -10,
        "synthetic": False,
        "batch_id": "c" * 32,
        "instance_id": "d" * 16,
        "seq": 7,
        "sdk": {
            "name": "tr-py",
            "version": "0.6.0",
            "lang": "python",
            "runtime": "cpython/3.12.1",
            "os": "linux",
            "arch": "x64",
        },
        "events": [
            _event(attempts),
            _event([_attempt(0)], sample_reason="random", sample_rate=0.01),
        ],
        "counters": [
            _counter(),
            _counter(level="attempt", attempts=1, first_attempt_success=0),
            _counter(host="ally", outcome="http_error", http_status_class="5xx"),
        ],
    }


def test_old_activity_payload_gets_exact_optional_defaults() -> None:
    payload = activity_payload(_generation())
    for field in ACTIVITY_OPTIONAL_DEFAULTS:
        payload.pop(field, None)
    row = OperationalOutboxRow(1, COMMIT_TS, "activity", "gen-old", json.dumps(payload))

    [event] = normalise_operational_event(row)

    assert tuple(key for key in event.row if key != "ingest_version") == ACTIVITY_COLUMNS
    assert {key: event.row[key] for key in ACTIVITY_OPTIONAL_DEFAULTS} == (
        ACTIVITY_OPTIONAL_DEFAULTS
    )


def test_activity_payload_explicit_nulls_get_clickhouse_string_defaults() -> None:
    payload = activity_payload(_generation())
    row = OperationalOutboxRow(1, COMMIT_TS, "activity", "gen-null", json.dumps(payload))

    [event] = normalise_operational_event(row)

    for field, default in ACTIVITY_OPTIONAL_DEFAULTS.items():
        if default is not None:
            assert event.row[field] == default
            assert type(event.row[field]) is type(default)


def test_old_activity_payload_still_rejects_missing_required_field() -> None:
    payload = activity_payload(_generation())
    payload.pop("generation_id")
    row = OperationalOutboxRow(1, COMMIT_TS, "activity", "gen-old", json.dumps(payload))

    with pytest.raises(ValueError, match="generation_id"):
        normalise_operational_event(row)


def test_client_events_expand_to_complete_deterministic_rows() -> None:
    payload = _client_payload()

    rows = expand_client_events_payload(payload, COMMIT_TS)

    assert len(rows) == 5
    requests = [event for event in rows if event.event_kind == "client_request"]
    counters = [event for event in rows if event.event_kind == "client_counter"]
    assert len(requests) == 2
    assert len(counters) == 3
    expected_request_id = hashlib.sha256(f"{'a' * 64}:{'c' * 32}:r:0".encode()).hexdigest()
    expected_counter_id = hashlib.sha256(f"{'a' * 64}:{'c' * 32}:c:2".encode()).hexdigest()
    assert requests[0].row["event_id"] == expected_request_id
    assert counters[2].row["event_id"] == expected_counter_id
    assert requests[0].row["attempt_http_status"] == [0, 503, 200]
    assert requests[0].row["attempt_error_class"] == ["connect_timeout", "", ""]
    assert requests[0].row["attempt_error_source"] == ["", "router", ""]
    assert requests[0].row["attempt_should_retry"] == ["true", "false", "absent"]
    assert requests[0].row["attempt_retry_after_ms"] == [0, 0, 0]
    assert requests[0].row["attempt_ttfb_ms"] == [0, 50, 50]
    assert requests[0].row["attempt_request_id"] == [
        "",
        "rlog_" + "a" * 32,
        "",
    ]
    assert requests[0].row["first_error_class"] == "connect_timeout"
    assert requests[0].row["error_source"] == "router"
    assert requests[0].row["final_host"] == "uptime"
    assert set(requests[0].row) == {*CLIENT_REQUEST_COLUMNS, "ingest_version"}
    assert set(counters[0].row) == {*CLIENT_COUNTER_COLUMNS, "ingest_version"}


class _Source:
    def __init__(self, rows: list[OperationalOutboxRow]) -> None:
        self.rows = rows
        self.deleted: list[OperationalOutboxRow] = []

    def fetch(self, *, limit: int) -> list[OperationalOutboxRow]:
        return self.rows[:limit]

    def fetch_shard(self, shard: int, *, limit: int) -> list[OperationalOutboxRow]:
        return [row for row in self.rows if row.shard == shard][:limit]

    def delete(self, rows: list[OperationalOutboxRow]) -> int:
        self.deleted.extend(rows)
        self.rows = [row for row in self.rows if row not in rows]
        return len(rows)

    def oldest_commit_ts(self) -> dt.datetime | None:
        return self.rows[0].commit_ts if self.rows else None


class _Writer:
    def __init__(self) -> None:
        self.batches: list[list[CanonicalOperationalEvent]] = []

    def insert(self, events: list[CanonicalOperationalEvent]) -> None:
        self.batches.append(events)


def _activity_row(*, shard: int = 1) -> OperationalOutboxRow:
    return OperationalOutboxRow(
        shard,
        COMMIT_TS,
        "activity",
        "gen-good",
        json.dumps(activity_payload(_generation())),
    )


def _poison_row(*, shard: int = 1) -> OperationalOutboxRow:
    return OperationalOutboxRow(shard, COMMIT_TS, "unknown", "bad-1", '{"x":1}')


def test_spanner_drain_quarantines_unknown_kind_and_continues() -> None:
    source = _Source([_poison_row(), _activity_row()])
    writer = _Writer()

    result = drain_once(source, writer, batch_size=10)

    assert result.inserted == 1
    assert result.quarantined == 1
    assert source.rows == []
    assert [event.event_kind for event in writer.batches[0]] == [
        "quarantine",
        "activity",
    ]
    quarantine = writer.batches[0][0].row
    assert quarantine["event_kind"] == "unknown"
    assert "unsupported operational event kind" in quarantine["reason"]


def test_postgres_drain_quarantines_unknown_kind_and_continues() -> None:
    source = _Source([_poison_row(), _activity_row()])
    writer = _Writer()

    result = drain_shard_once(source, writer, shard=1, batch_size=10)

    assert result.inserted == 1
    assert result.quarantined == 1
    assert result.deleted == 2
    assert source.rows == []


def test_writer_rejects_a_table_outside_its_allowlist() -> None:
    event = CanonicalOperationalEvent(event_kind="secrets", row={"value": "no"})

    with pytest.raises(ValueError, match="unsupported operational event kind"):
        ClickHouseOperationalWriter(password="test").insert([event])  # noqa: S106


def test_client_event_schemas_are_bounded_and_column_aligned() -> None:
    replicated = (ROOT / "clickhouse/008_client_events_replicated.sql").read_text()
    single = (ROOT / "clickhouse/009_client_events_single_node.sql").read_text()

    assert "INTERVAL 90 DAY" in replicated
    assert "INTERVAL 180 DAY" in replicated
    assert "INTERVAL 24 MONTH" in replicated
    assert "INTERVAL 30 DAY" in replicated
    assert "deliberately NOT archived" in replicated
    assert replicated.count("ENGINE = ReplicatedReplacingMergeTree") == 3
    assert "ENGINE = ReplicatedMergeTree" in replicated
    assert single.count("ENGINE = ReplacingMergeTree") == 3
    assert "ENGINE = MergeTree" in single
    for column in ACTIVITY_OPTIONAL_DEFAULTS:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in replicated


def test_normalise_client_events_returns_the_expansion() -> None:
    row = OperationalOutboxRow(
        1,
        COMMIT_TS,
        "client_events",
        "batch-1",
        json.dumps(_client_payload()),
    )

    assert normalise_operational_event(row) == expand_client_events_payload(
        _client_payload(), COMMIT_TS
    )


def test_quarantine_reason_is_bounded() -> None:
    source = _Source([dataclasses.replace(_poison_row(), event_kind="x" * 600)])
    writer = _Writer()

    drain_once(source, writer, batch_size=1)

    assert len(writer.batches[0][0].row["reason"]) == 500


def _ddl_columns(ddl: str, table: str) -> tuple[str, ...]:
    """Column names of one CREATE TABLE block, in declaration order."""
    import re

    start = ddl.index(f"CREATE TABLE IF NOT EXISTS {table}\n(")
    body = ddl[start:].split("\n)\n", 1)[0].splitlines()[2:]
    return tuple(
        match.group(1)
        for line in body
        if (match := re.match(r"^\s+([a-z_]+)\s+\S", line)) is not None
    )


def test_ingester_column_tuples_match_the_ddl_in_order() -> None:
    """The ingester projects rows by these tuples; the DDL must agree exactly.

    A drifted or reordered column would still insert (JSONEachRow is by name)
    until a NEW column lands in one place only -- then the drain fails on
    every client_events row. Pin the two lists to each other, replicated and
    single-node alike.
    """
    for ddl in (
        (ROOT / "clickhouse/008_client_events_replicated.sql").read_text(),
        (ROOT / "clickhouse/009_client_events_single_node.sql").read_text(),
    ):
        assert _ddl_columns(ddl, "client_request_events") == (
            *CLIENT_REQUEST_COLUMNS,
            "ingest_version",
        )
        assert _ddl_columns(ddl, "client_minute_counters") == (
            *CLIENT_COUNTER_COLUMNS,
            "ingest_version",
        )
        assert _ddl_columns(ddl, "operational_outbox_quarantine") == (
            "shard",
            "commit_ts",
            "event_kind",
            "event_id",
            "payload",
            "reason",
            "quarantined_at",
        )
        activity_alters = [
            line.split("ADD COLUMN IF NOT EXISTS ")[1].split()[0]
            for line in ddl.splitlines()
            if "ADD COLUMN IF NOT EXISTS" in line
        ]
        assert tuple(activity_alters) == tuple(ACTIVITY_OPTIONAL_DEFAULTS)
        assert ACTIVITY_COLUMNS[-len(activity_alters) :] == tuple(activity_alters)
