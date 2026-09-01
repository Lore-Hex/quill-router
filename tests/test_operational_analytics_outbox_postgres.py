"""Contract for the Postgres/DSQL operational-analytics outbox and its drain.

Three properties are load-bearing and each is pinned here rather than left to
review:

* **Privacy.** ClickHouse must receive surrogates and content-free metadata.
  A raw workspace id or key hash reaching the payload is a leak no downstream
  fix can undo, so the projection is asserted against the encoded bytes, not
  just the dict keys.
* **Idempotency.** Aurora DSQL aborts on OCC (SQLSTATE 40001) and the store
  retries the whole transaction, so an enqueue WILL be replayed. The primary
  key has to absorb that.
* **At-least-once delivery.** The drain writes to ClickHouse before deleting.
  The window between those two is a duplicate — which the ReplacingMergeTree
  collapses — and must never be a loss.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
import sqlite3
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clickhouse.ingest_operational_outbox import OperationalOutboxRow
from clickhouse.ingest_operational_outbox_postgres import (
    RETRYABLE_SQLSTATES,
    ShardDrainResult,
    drain_once,
    drain_shard_once,
    retry_serialization,
)
from trusted_router.storage_models import (
    GatewayAuthorization,
    Generation,
    SyntheticProbeSample,
)
from trusted_router.storage_operational_analytics import (
    ACTIVITY_EVENT_KIND,
    OPERATIONAL_ANALYTICS_OUTBOX_SHARDS,
    SYNTHETIC_EVENT_KIND,
    activity_payload,
    analytics_surrogate,
    operational_analytics_shard,
    synthetic_payload,
)
from trusted_router.storage_postgres_operational_analytics_outbox import (
    OUTBOX_TABLE,
    PostgresOperationalAnalyticsOutbox,
)
from trusted_router.types import UsageType

# --------------------------------------------------------------------------
# Fixtures / fakes
# --------------------------------------------------------------------------


def _generation(generation_id: str = "gen-pg-activity-1") -> Generation:
    return Generation(
        id=generation_id,
        request_id="req-pg-activity-1",
        workspace_id="ws-private-123",
        key_hash="salted-key-hash-private",
        model="anthropic/claude-haiku-4.5",
        provider="anthropic",
        provider_name="Anthropic",
        app="Test app",
        tokens_prompt=12,
        tokens_completion=3,
        total_cost_microdollars=9,
        usage_type=UsageType.CREDITS,
        speed_tokens_per_second=7.5,
        finish_reason="stop",
        status="success",
        streamed=True,
        usage_estimated=False,
        cached_input_tokens=2,
        reasoning_tokens=1,
        tool_calls=[{"function": {"arguments": "private model output"}}],
        operator_cost_microdollars=7,
        tags={"team": "legal"},
        created_at="2026-07-31T12:34:56.789Z",
    )


def _sample(sample_id: str = "probe-pg-1") -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=sample_id,
        probe_type="gateway",
        target="api-aws",
        target_url="https://api-aws.trustedrouter.com/v1",
        monitor_region="eu-west-3",
        status="up",
        latency_milliseconds=50,
        ttfb_milliseconds=25,
        created_at="2026-07-31T12:34:56.789Z",
    )


class _FakeCursor:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeConn:
    """A connection that actually enforces the outbox primary key.

    A fake that merely recorded SQL would happily "pass" an idempotency test
    while the real table duplicated rows, so this one keeps a keyed dict and
    honours ON CONFLICT DO NOTHING.
    """

    def __init__(self) -> None:
        self.rows: dict[tuple[int, str, str], str] = {}
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    def execute(self, sql: str, params: tuple[Any, ...] = (), **kwargs: Any) -> _FakeCursor:
        self.statements.append((sql, params))
        if sql.startswith(f"INSERT INTO {OUTBOX_TABLE}"):
            assert "ON CONFLICT (shard, event_kind, event_id) DO NOTHING" in sql
            shard, event_kind, event_id, payload = params
            key = (int(shard), str(event_kind), str(event_id))
            if key in self.rows:
                return _FakeCursor(0)
            self.rows[key] = str(payload)
            return _FakeCursor(1)
        raise AssertionError(f"unexpected statement: {sql}")

    @property
    def outbox_payloads(self) -> list[dict[str, Any]]:
        return [json.loads(payload) for payload in self.rows.values()]


def _outbox(conn: _FakeConn) -> PostgresOperationalAnalyticsOutbox:
    """An outbox whose implicit transaction runs on `conn`.

    Mirrors PostgresStore._run_transaction, which hands the operation a
    connection already inside `conn.transaction()`.
    """

    def run_transaction(operation: Any) -> Any:
        return operation(conn)

    return PostgresOperationalAnalyticsOutbox(run_transaction)


# --------------------------------------------------------------------------
# Privacy
# --------------------------------------------------------------------------


def test_enqueued_activity_payload_names_its_workspace_but_never_keys() -> None:
    """Same 2026-08-19 boundary as the Spanner side, asserted on the AWS path
    so the two clouds cannot drift apart: workspace_id rides in the row, key
    hashes and content stay out."""
    conn = _FakeConn()
    generation = _generation()

    _outbox(conn).enqueue_activity_tx(conn, generation)

    [(_, params)] = conn.statements
    encoded = params[3]
    payload = json.loads(encoded)
    assert payload["tenant_id"] == analytics_surrogate("workspace", generation.workspace_id)
    assert payload["workspace_id"] == generation.workspace_id
    assert payload["key_id"] == analytics_surrogate("api-key", generation.key_hash)
    # Assert on the encoded bytes: a raw key hash could otherwise hide in a
    # nested structure the key assertions above never look at.
    assert generation.key_hash not in encoded
    assert "private model output" not in encoded
    assert "tool_calls" not in payload
    assert "operator_cost_microdollars" not in payload
    assert "prompt_content" not in payload
    assert "output_content" not in payload


def test_enqueued_synthetic_payload_is_the_public_projection() -> None:
    conn = _FakeConn()
    sample = _sample()

    _outbox(conn).enqueue_synthetic_tx(conn, sample)

    assert conn.outbox_payloads == [synthetic_payload(sample)]


def test_postgres_and_spanner_share_one_payload_projection() -> None:
    """The privacy contract is one implementation, not two that agree today."""
    from trusted_router import storage_gcp_operational_analytics_outbox as gcp

    assert gcp.activity_payload is activity_payload
    assert gcp.synthetic_payload is synthetic_payload
    assert gcp.analytics_surrogate is analytics_surrogate
    assert gcp.operational_analytics_shard is operational_analytics_shard
    assert gcp.OPERATIONAL_ANALYTICS_OUTBOX_SHARDS == OPERATIONAL_ANALYTICS_OUTBOX_SHARDS
    assert gcp.ACTIVITY_EVENT_KIND == ACTIVITY_EVENT_KIND
    assert gcp.SYNTHETIC_EVENT_KIND == SYNTHETIC_EVENT_KIND


# --------------------------------------------------------------------------
# Idempotency
# --------------------------------------------------------------------------


def test_double_enqueue_of_one_event_leaves_exactly_one_row() -> None:
    conn = _FakeConn()
    generation = _generation()
    outbox = _outbox(conn)

    outbox.enqueue_activity_tx(conn, generation)
    outbox.enqueue_activity_tx(conn, generation)

    assert len(conn.statements) == 2
    assert set(conn.rows) == {
        (
            operational_analytics_shard(f"{ACTIVITY_EVENT_KIND}:{generation.id}"),
            ACTIVITY_EVENT_KIND,
            generation.id,
        )
    }


def test_activity_and_synthetic_sharing_an_id_are_distinct_events() -> None:
    """event_kind is part of the key, so the two streams cannot collide."""
    conn = _FakeConn()
    outbox = _outbox(conn)
    shared_id = "collide-1"

    outbox.enqueue_activity_tx(conn, _generation(shared_id))
    outbox.enqueue_synthetic_tx(conn, _sample(shared_id))

    assert {kind for _, kind, _ in conn.rows} == {
        ACTIVITY_EVENT_KIND,
        SYNTHETIC_EVENT_KIND,
    }


def test_enqueue_without_a_caller_transaction_uses_the_stores_runner() -> None:
    conn = _FakeConn()
    calls: list[str] = []

    def run_transaction(operation: Any) -> Any:
        calls.append("ran")
        return operation(conn)

    PostgresOperationalAnalyticsOutbox(run_transaction).enqueue_synthetic(_sample())

    assert calls == ["ran"]
    assert conn.outbox_payloads == [synthetic_payload(_sample())]


# --------------------------------------------------------------------------
# Sharding
# --------------------------------------------------------------------------


def test_sharding_is_deterministic_and_within_range() -> None:
    for index in range(500):
        event_id = f"{ACTIVITY_EVENT_KIND}:gen-{index}"
        shard = operational_analytics_shard(event_id)
        assert shard == operational_analytics_shard(event_id)
        assert 0 <= shard < OPERATIONAL_ANALYTICS_OUTBOX_SHARDS

    # A hash that collapsed onto one shard would serialize the whole drain.
    spread = {
        operational_analytics_shard(f"{ACTIVITY_EVENT_KIND}:gen-{index}")
        for index in range(500)
    }
    assert len(spread) == OPERATIONAL_ANALYTICS_OUTBOX_SHARDS

    with pytest.raises(ValueError, match="shard_count must be positive"):
        operational_analytics_shard("activity:gen-1", shard_count=0)


def test_enqueue_writes_the_shard_the_drain_will_read() -> None:
    conn = _FakeConn()
    generation = _generation()

    _outbox(conn).enqueue_activity_tx(conn, generation)

    [(shard, event_kind, event_id)] = list(conn.rows)
    assert event_kind == ACTIVITY_EVENT_KIND
    assert event_id == generation.id
    assert shard == operational_analytics_shard(f"{ACTIVITY_EVENT_KIND}:{generation.id}")


def test_custom_shard_count_must_be_positive() -> None:
    with pytest.raises(ValueError, match="shard_count must be positive"):
        PostgresOperationalAnalyticsOutbox(lambda operation: None, shard_count=0)


def test_shard_count_override_is_honoured_by_the_writer() -> None:
    conn = _FakeConn()
    generation = _generation()

    def run_transaction(operation: Any) -> Any:
        return operation(conn)

    PostgresOperationalAnalyticsOutbox(run_transaction, shard_count=4).enqueue_activity(
        generation
    )

    [(shard, _, _)] = list(conn.rows)
    assert shard == operational_analytics_shard(
        f"{ACTIVITY_EVENT_KIND}:{generation.id}", shard_count=4
    )
    assert 0 <= shard < 4


# --------------------------------------------------------------------------
# Drain: at-least-once
# --------------------------------------------------------------------------


class _FakeSource:
    """An outbox table for the drain, keyed exactly as Postgres keys it."""

    def __init__(self, rows: list[OperationalOutboxRow]) -> None:
        self.rows = list(rows)
        self.deleted: list[OperationalOutboxRow] = []
        self.fail_next_delete = False

    def fetch_shard(self, shard: int, *, limit: int) -> list[OperationalOutboxRow]:
        return [row for row in self.rows if row.shard == shard][:limit]

    def delete(self, rows: list[OperationalOutboxRow]) -> int:
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise RuntimeError("connection reset before DELETE committed")
        keys = {(row.shard, row.event_kind, row.event_id) for row in rows}
        self.deleted.extend(rows)
        before = len(self.rows)
        self.rows = [
            row
            for row in self.rows
            if (row.shard, row.event_kind, row.event_id) not in keys
        ]
        return before - len(self.rows)


class _FakeWriter:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.batches: list[list[Any]] = []

    def insert(self, events: list[Any]) -> None:
        if self.failures:
            self.failures -= 1
            raise RuntimeError("ClickHouse unavailable")
        self.batches.append(events)


def _row(event_id: str, *, shard: int | None = None) -> OperationalOutboxRow:
    payload = activity_payload(_generation(event_id))
    return OperationalOutboxRow(
        shard=(
            operational_analytics_shard(f"{ACTIVITY_EVENT_KIND}:{event_id}")
            if shard is None
            else shard
        ),
        commit_ts=dt.datetime(2026, 7, 31, 12, 35, tzinfo=dt.UTC),
        event_kind=ACTIVITY_EVENT_KIND,
        event_id=event_id,
        payload=json.dumps(payload),
    )


def test_drain_never_deletes_a_row_clickhouse_did_not_accept() -> None:
    row = _row("gen-unaccepted")
    source = _FakeSource([row])
    writer = _FakeWriter(failures=1)

    with pytest.raises(RuntimeError, match="ClickHouse unavailable"):
        drain_shard_once(source, writer, shard=row.shard, batch_size=10)

    assert source.deleted == []
    assert source.rows == [row]
    assert writer.batches == []

    result = drain_shard_once(source, writer, shard=row.shard, batch_size=10)
    assert result == ShardDrainResult(shard=row.shard, fetched=1, inserted=1, deleted=1)
    assert source.rows == []


def test_failure_after_the_clickhouse_write_redelivers_rather_than_losing() -> None:
    """The at-least-once window: written, not yet deleted, process dies."""
    row = _row("gen-redelivered")
    source = _FakeSource([row])
    writer = _FakeWriter()
    source.fail_next_delete = True

    with pytest.raises(RuntimeError, match="before DELETE committed"):
        drain_shard_once(source, writer, shard=row.shard, batch_size=10)

    # ClickHouse HAS the row and the outbox still HAS the row. That is the
    # duplicate the ReplacingMergeTree collapses, and it is the correct
    # trade: deleting first would have made this a permanent loss.
    assert len(writer.batches) == 1
    assert source.rows == [row]

    drain_shard_once(source, writer, shard=row.shard, batch_size=10)

    assert len(writer.batches) == 2
    assert writer.batches[0][0].row == writer.batches[1][0].row
    assert source.rows == []


def test_drain_writes_before_it_deletes() -> None:
    """Order is the whole safety property, so assert the order directly."""
    order: list[str] = []
    row = _row("gen-ordered")

    class _OrderedSource(_FakeSource):
        def delete(self, rows: list[OperationalOutboxRow]) -> int:
            order.append("delete")
            return super().delete(rows)

    class _OrderedWriter(_FakeWriter):
        def insert(self, events: list[Any]) -> None:
            order.append("insert")
            super().insert(events)

    drain_shard_once(
        _OrderedSource([row]),
        _OrderedWriter(),
        shard=row.shard,
        batch_size=10,
    )

    assert order == ["insert", "delete"]


def test_drain_bounds_each_batch_and_leaves_the_remainder_queued() -> None:
    rows = [_row(f"gen-batch-{index}", shard=7) for index in range(6)]
    source = _FakeSource(rows)
    writer = _FakeWriter()

    result = drain_shard_once(source, writer, shard=7, batch_size=2)

    assert result.fetched == 2
    assert result.deleted == 2
    assert len(source.rows) == 4
    assert len(writer.batches[0]) == 2


def test_drain_once_sweeps_every_shard() -> None:
    rows = [_row(f"gen-sweep-{index}") for index in range(40)]
    source = _FakeSource(rows)
    writer = _FakeWriter()

    result = drain_once(source, writer, batch_size=100)

    assert result.fetched == len(rows)
    assert result.inserted == len(rows)
    assert source.rows == []
    assert {row.shard for row in source.deleted} == {row.shard for row in rows}


def test_empty_shard_touches_neither_clickhouse_nor_delete() -> None:
    source = _FakeSource([])
    writer = _FakeWriter()

    result = drain_shard_once(source, writer, shard=0, batch_size=10)

    assert result == ShardDrainResult(shard=0, fetched=0, inserted=0, deleted=0)
    assert writer.batches == []
    assert source.deleted == []


def test_drain_delete_names_only_the_rows_it_fetched() -> None:
    """A row enqueued mid-drain must survive, so the delete is key-scoped."""
    drained = _row("gen-drained", shard=3)
    arrived_late = _row("gen-arrived-late", shard=3)
    writer = _FakeWriter()

    class _RaceSource(_FakeSource):
        def fetch_shard(self, shard: int, *, limit: int) -> list[OperationalOutboxRow]:
            fetched = super().fetch_shard(shard, limit=limit)
            self.rows.append(arrived_late)
            return fetched

    source = _RaceSource([drained])
    drain_shard_once(source, writer, shard=3, batch_size=10)

    assert source.rows == [arrived_late]
    assert source.deleted == [drained]


# --------------------------------------------------------------------------
# Drain: OCC retry
# --------------------------------------------------------------------------


class _Rollback(Exception):
    def __init__(self, sqlstate: str) -> None:
        super().__init__(sqlstate)
        self.sqlstate = sqlstate


def test_serialization_failures_are_retried_and_other_errors_are_not() -> None:
    assert "40001" in RETRYABLE_SQLSTATES

    attempts = 0

    def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise _Rollback("40001")
        return "committed"

    assert retry_serialization(flaky, sleep=lambda _seconds: None) == "committed"
    assert attempts == 3

    def unique_violation() -> str:
        raise _Rollback("23505")

    with pytest.raises(_Rollback):
        retry_serialization(unique_violation, sleep=lambda _seconds: None)


def test_exhausted_retries_raise_rather_than_silently_skipping_rows() -> None:
    def always_aborts() -> None:
        raise _Rollback("40001")

    with pytest.raises(RuntimeError, match="rolled back"):
        retry_serialization(always_aborts, attempts=3, sleep=lambda _seconds: None)


def test_clickhouse_errors_are_not_swallowed_by_the_occ_retry() -> None:
    """A ClickHouse failure must reach the caller so the DELETE is skipped."""

    def writer_failure() -> None:
        raise RuntimeError("ClickHouse unavailable")

    with pytest.raises(RuntimeError, match="ClickHouse unavailable"):
        retry_serialization(writer_failure, sleep=lambda _seconds: None)


# --------------------------------------------------------------------------
# Store wiring
# --------------------------------------------------------------------------


def _authorization() -> GatewayAuthorization:
    return GatewayAuthorization(
        id="auth-pg-1",
        workspace_id="ws-private-123",
        key_hash="salted-key-hash-private",
        model_id="anthropic/claude-haiku-4.5",
        provider="anthropic",
        usage_type=UsageType.CREDITS,
        estimated_microdollars=100,
        credit_reservation_id=None,
    )


def _finalizing_store(conn: _FakeConn, *, outbox_enabled: bool) -> tuple[Any, _FakeConn]:
    """A PostgresStore with only the collaborators finalize touches.

    Constructed without __init__ so no pool is opened; every method the
    settle path calls is stubbed EXCEPT the outbox wiring under test, so the
    real `finalize_gateway_authorization` body runs.

    The outbox's *own* transaction runner is deliberately pointed at a DIFFERENT
    connection than the one the settle runs on. That difference is what makes
    "the enqueue joins the caller's transaction" a testable claim: wiring both
    to one connection — as this fixture originally did — makes
    `enqueue_activity_tx(conn, ...)` and `enqueue_activity(...)` land in the
    same place, so a swap between them is invisible. Here the non-transactional
    call lands on `other` and the assertions on `conn` fail.
    """
    import trusted_router.storage_postgres as storage_postgres

    other = _FakeConn()
    store = object.__new__(storage_postgres.PostgresStore)
    store._operational_analytics_outbox = _outbox(other) if outbox_enabled else None
    store._run_transaction = lambda operation: operation(conn)
    store._read_entity_tx = lambda *args, **kwargs: _authorization()
    store._insert_entity_once_tx = lambda *args, **kwargs: True
    store._write_entity_tx = lambda *args, **kwargs: None
    store._write_indexed_entity_tx = lambda *args, **kwargs: None
    store._release_key_hold_tx = lambda *args, **kwargs: None
    return store, other


def test_settle_enqueues_activity_on_the_settling_connection() -> None:
    """Settlement and delivery intent must be one transaction, not two."""
    conn = _FakeConn()
    store, other = _finalizing_store(conn, outbox_enabled=True)
    generation = _generation()

    assert (
        store.finalize_gateway_authorization(
            "auth-pg-1",
            success=True,
            actual_microdollars=0,
            selected_usage_type=UsageType.CREDITS,
            generation=generation,
        )
        is True
    )

    # The INSERT landed on the very connection the settle ran on. `other` is
    # where the outbox would write if it opened its own transaction, so its
    # emptiness is the real assertion: in production that second transaction is
    # a second pool checkout (deadlocking a 4-connection pool under concurrent
    # settles) that commits the activity event independently of the money.
    assert list(conn.rows) == [
        (
            operational_analytics_shard(f"{ACTIVITY_EVENT_KIND}:{generation.id}"),
            ACTIVITY_EVENT_KIND,
            generation.id,
        )
    ]
    assert other.rows == {}, "enqueue opened its own transaction instead of joining the settle"


def test_settle_without_a_generation_enqueues_nothing() -> None:
    conn = _FakeConn()
    store, other = _finalizing_store(conn, outbox_enabled=True)

    store.finalize_gateway_authorization(
        "auth-pg-1",
        success=True,
        actual_microdollars=0,
        selected_usage_type=UsageType.CREDITS,
        generation=None,
    )

    assert conn.rows == {}
    assert other.rows == {}


def test_settle_with_the_outbox_disabled_writes_no_outbox_row() -> None:
    conn = _FakeConn()
    store, other = _finalizing_store(conn, outbox_enabled=False)

    store.finalize_gateway_authorization(
        "auth-pg-1",
        success=True,
        actual_microdollars=0,
        selected_usage_type=UsageType.CREDITS,
        generation=_generation(),
    )

    assert conn.rows == {}
    assert other.rows == {}


def test_recording_a_synthetic_probe_enqueues_it_on_the_recording_connection() -> None:
    """The synthetic half of the feature, through the store that owns it.

    Without this the whole `record_synthetic_probe_sample` enqueue could be
    deleted and every test still passed: the outbox's own tests call
    `enqueue_synthetic_tx` directly and never reach the store. That mutant
    ships an AWS deployment where activity_generations fills and
    synthetic_probe_samples stays permanently at zero rows.
    """
    conn = _FakeConn()
    store, other = _finalizing_store(conn, outbox_enabled=True)
    # Returning False short-circuits the rollup loop at its first marker
    # insert, leaving the sample write and its delivery intent as the subject.
    store._insert_entity_once_tx = lambda *args, **kwargs: False
    sample = _sample()

    store.record_synthetic_probe_sample(sample)

    assert list(conn.rows) == [
        (
            operational_analytics_shard(f"{SYNTHETIC_EVENT_KIND}:{sample.id}"),
            SYNTHETIC_EVENT_KIND,
            sample.id,
        )
    ]
    assert conn.outbox_payloads == [synthetic_payload(sample)]
    assert other.rows == {}, "enqueue opened its own transaction instead of joining the record"


def test_synthetic_probe_with_the_outbox_disabled_writes_no_outbox_row() -> None:
    conn = _FakeConn()
    store, _other = _finalizing_store(conn, outbox_enabled=False)
    store._insert_entity_once_tx = lambda *args, **kwargs: False

    store.record_synthetic_probe_sample(_sample())

    assert conn.rows == {}


def test_disabled_flag_leaves_the_outbox_unwired(monkeypatch: pytest.MonkeyPatch) -> None:
    import trusted_router.storage_postgres as storage_postgres

    class _FakePool:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def close(self) -> None:
            pass

    monkeypatch.setattr(storage_postgres, "ConnectionPool", _FakePool)

    default = storage_postgres.PostgresStore("postgresql://postgres.example/test")
    enabled = storage_postgres.PostgresStore(
        "postgresql://postgres.example/test",
        operational_analytics_outbox_enabled=True,
    )
    try:
        assert default._operational_analytics_outbox is None
        assert enabled._operational_analytics_outbox is not None
    finally:
        default.close()
        enabled.close()


def test_create_store_passes_the_outbox_flag_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    import trusted_router.storage_postgres as storage_postgres
    from trusted_router.storage import create_store

    captured: dict[str, Any] = {}

    class _FakeStore:
        def __init__(self, dsn: str, **kwargs: Any) -> None:
            captured.update(kwargs)

        def apply_schema(self) -> None:
            return None

    monkeypatch.setattr(storage_postgres, "PostgresStore", _FakeStore)
    create_store(
        SimpleNamespace(
            storage_backend="postgres",
            postgres_dsn="postgresql://admin@cluster.example/postgres",
            operational_analytics_outbox_enabled=True,
        )
    )

    assert captured["operational_analytics_outbox_enabled"] is True


# --------------------------------------------------------------------------
# The statements against the real DDL
#
# Everything above drives fakes. A fake cannot disagree with the schema file,
# so nothing above would notice the INSERT's ON CONFLICT target drifting from
# the table's PRIMARY KEY -- a mismatch real Postgres rejects with 42P10, and
# which, because the INSERT rides the settle transaction, would fail EVERY
# gateway settlement in the region.
#
# So these run the production SQL constants against the production CREATE TABLE
# text in SQLite. SQLite is dynamically typed and accepts the DDL verbatim
# (BIGINT/TEXT/TIMESTAMPTZ are all valid column type names to it), it enforces
# composite primary keys, and it rejects an ON CONFLICT clause that matches no
# unique constraint -- which are exactly the properties under test.
# --------------------------------------------------------------------------


# Literal rather than interpolated so these read as fixed statements; the
# assertion below keeps them tied to the table the production code names.
_SEED_ROW_SQL = (
    "INSERT INTO tr_operational_analytics_outbox "
    "(shard, event_kind, event_id, payload, enqueued_at) VALUES (%s, %s, %s, %s, %s)"
)
_READ_KEYS_SQL = "SELECT shard, event_kind, event_id FROM tr_operational_analytics_outbox"

assert OUTBOX_TABLE in _SEED_ROW_SQL
assert OUTBOX_TABLE in _READ_KEYS_SQL


def _outbox_schema_statements() -> list[str]:
    """The outbox DDL, taken from the file the store actually applies."""
    import trusted_router.storage_postgres as storage_postgres

    schema = Path(storage_postgres.__file__).with_name("storage_postgres_schema.sql").read_text()
    # Reuse the production splitter so the test sees the same statements
    # apply_schema() would send.
    return [
        statement
        for statement in storage_postgres._split_sql_statements(schema)
        if OUTBOX_TABLE in statement
    ]


class _SqliteConn:
    """Psycopg-shaped adapter over SQLite, for running the real statements."""

    def __init__(self, raw: sqlite3.Connection) -> None:
        self._raw = raw
        self.closed = False
        self.fail_delete_after: int | None = None
        self._deletes = 0

    def execute(self, sql: str, params: tuple[Any, ...] = (), **kwargs: Any) -> Any:
        if sql.startswith("DELETE"):
            self._deletes += 1
            if self.fail_delete_after is not None and self._deletes > self.fail_delete_after:
                raise RuntimeError("connection reset mid-DELETE")
        return self._raw.execute(sql.replace("%s", "?"), params)

    # psycopg's `with conn.transaction()` is all-or-nothing; so is this.
    def transaction(self) -> Any:
        import contextlib

        @contextlib.contextmanager
        def _txn() -> Any:
            self._raw.execute("BEGIN")
            try:
                yield self
            except BaseException:
                self._raw.execute("ROLLBACK")
                raise
            self._raw.execute("COMMIT")

        return _txn()

    def close(self) -> None:
        # Marks the handle discarded without destroying the database, so a test
        # can still inspect the rows after the source drops the connection.
        self.closed = True

    def rows(self) -> set[tuple[int, str, str]]:
        return {
            (int(shard), str(kind), str(event_id))
            for shard, kind, event_id in self._raw.execute(_READ_KEYS_SQL)
        }


def _real_schema_conn() -> _SqliteConn:
    sqlite3.register_converter("TIMESTAMPTZ", lambda raw: dt.datetime.fromisoformat(raw.decode()))
    raw = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
    raw.isolation_level = None  # explicit BEGIN/COMMIT, like psycopg
    for statement in _outbox_schema_statements():
        raw.execute(statement)
    return _SqliteConn(raw)


def test_the_outbox_ddl_is_actually_in_the_schema_file() -> None:
    """Guards the tests below: they prove nothing if they build no table."""
    statements = _outbox_schema_statements()
    assert any(statement.startswith(f"CREATE TABLE IF NOT EXISTS {OUTBOX_TABLE}") for statement in statements)


def test_insert_statement_matches_the_tables_real_primary_key() -> None:
    """ON CONFLICT must name a key the DDL actually declares.

    If the two drift, Postgres raises 42P10 on the INSERT. That INSERT runs
    inside `finalize_gateway_authorization`, where `_run_transaction` turns any
    psycopg.Error into StoreUnavailable -- so the drift does not degrade
    analytics, it fails every settlement and strands credit holds.
    """
    conn = _real_schema_conn()
    generation = _generation()

    PostgresOperationalAnalyticsOutbox(lambda operation: operation(conn)).enqueue_activity_tx(
        conn, generation
    )

    expected = (
        operational_analytics_shard(f"{ACTIVITY_EVENT_KIND}:{generation.id}"),
        ACTIVITY_EVENT_KIND,
        generation.id,
    )
    assert conn.rows() == {expected}


def test_replayed_enqueue_collapses_on_the_real_key_rather_than_duplicating() -> None:
    """The OCC-retry guarantee, enforced by the real constraint."""
    conn = _real_schema_conn()
    outbox = PostgresOperationalAnalyticsOutbox(lambda operation: operation(conn))
    generation = _generation()

    outbox.enqueue_activity_tx(conn, generation)
    outbox.enqueue_activity_tx(conn, generation)

    assert len(conn.rows()) == 1


def _seeded_source(conn: _SqliteConn, rows: list[OperationalOutboxRow]) -> Any:
    from clickhouse.ingest_operational_outbox_postgres import (
        PostgresOperationalOutboxSource,
    )

    for row in rows:
        conn.execute(
            _SEED_ROW_SQL,
            (row.shard, row.event_kind, row.event_id, row.payload, row.commit_ts.isoformat()),
        )
    source = PostgresOperationalOutboxSource(dsn="postgresql://unused.example/db")
    source._connection = conn
    return source


def test_real_delete_names_only_the_rows_it_fetched() -> None:
    """A row enqueued mid-drain must survive -- against the real DELETE.

    The fake-source version of this test asserts a Python set comprehension.
    This one runs DELETE_BY_KEY_SQL itself, so widening it to a shard-wide
    delete (the natural "optimisation") fails here instead of silently
    dropping every event that arrived during the ClickHouse write.
    """
    conn = _real_schema_conn()
    drained = _row("gen-real-drained", shard=3)
    source = _seeded_source(conn, [drained])

    fetched = source.fetch_shard(3, limit=10)
    assert [row.event_id for row in fetched] == ["gen-real-drained"]

    arrived_late = _row("gen-real-late", shard=3)
    conn.execute(
        _SEED_ROW_SQL,
        (
            arrived_late.shard,
            arrived_late.event_kind,
            arrived_late.event_id,
            arrived_late.payload,
            arrived_late.commit_ts.isoformat(),
        ),
    )

    assert source.delete(fetched) == 1
    assert conn.rows() == {(3, ACTIVITY_EVENT_KIND, "gen-real-late")}


def test_a_partial_delete_rolls_back_whole_rather_than_losing_half_the_batch() -> None:
    """The batch DELETE is one transaction, so a mid-loop failure loses nothing.

    `delete()` issues one statement per row. Without the surrounding
    `conn.transaction()` the rows before the failure would be gone while the
    caller saw an exception and redelivered only the remainder -- silent loss
    inside the very window the at-least-once design exists to protect.
    """
    conn = _real_schema_conn()
    rows = [_row(f"gen-atomic-{index}", shard=5) for index in range(4)]
    source = _seeded_source(conn, rows)

    fetched = source.fetch_shard(5, limit=10)
    assert len(fetched) == 4
    conn.fail_delete_after = 2

    with pytest.raises(RuntimeError, match="mid-DELETE"):
        source.delete(fetched)

    assert len(conn.rows()) == 4, "a partially applied DELETE lost rows"


def test_oldest_enqueued_at_reads_the_real_table() -> None:
    conn = _real_schema_conn()
    old = _row("gen-old", shard=1)
    source = _seeded_source(conn, [old])

    assert source.oldest_enqueued_at() == old.commit_ts

    source.delete(source.fetch_shard(1, limit=10))
    assert source.oldest_enqueued_at() is None


# --------------------------------------------------------------------------
# Connecting: the DSN the control plane actually deploys
# --------------------------------------------------------------------------


def _fake_boto3(minted: dict[str, Any]) -> Any:
    class _Client:
        def generate_db_connect_admin_auth_token(self, **kwargs: Any) -> str:
            minted["admin"] = kwargs
            return "admin-token"

        def generate_db_connect_auth_token(self, **kwargs: Any) -> str:
            minted["scoped"] = kwargs
            return "scoped-token"

    return SimpleNamespace(client=lambda service, region_name: _Client())


def _connect_with(dsn: str, monkeypatch: pytest.MonkeyPatch) -> tuple[dict[str, Any], dict[str, Any]]:
    from clickhouse.ingest_operational_outbox_postgres import (
        PostgresOperationalOutboxSource,
    )

    minted: dict[str, Any] = {}
    captured: dict[str, Any] = {}
    monkeypatch.setitem(sys.modules, "boto3", _fake_boto3(minted))
    source = PostgresOperationalOutboxSource(dsn=dsn, iam_auth="aws-dsql")

    class _FakePsycopg:
        @staticmethod
        def connect(dsn: str, **kwargs: Any) -> str:
            captured.update(dsn=dsn, **kwargs)
            return "connection"

    source._psycopg = _FakePsycopg
    assert source._connect() == "connection"
    return minted, captured


def test_drain_connects_with_the_keyword_value_dsn_the_deployment_uses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The control plane deploys a libpq keyword/value DSN, not a URI.

    scripts/deploy/aws_eu_control_plane.sh sets
    `host=... port=5432 user=admin dbname=postgres sslmode=require`.
    Parsing that with urlsplit -- as this module originally did -- yields no
    hostname and raises before a token is ever minted, so the drain could never
    connect and ClickHouse stayed at zero rows no matter what else was fixed.
    """
    minted, captured = _connect_with(
        "host=cluster.dsql.eu-west-3.on.aws port=5432 user=admin dbname=postgres sslmode=require",
        monkeypatch,
    )

    assert minted["admin"]["Hostname"] == "cluster.dsql.eu-west-3.on.aws"
    assert minted["admin"]["Region"] == "eu-west-3"
    assert captured["password"] == "admin-token"  # noqa: S105 - fake token, not a secret


def test_drain_still_accepts_the_uri_dsn_form(monkeypatch: pytest.MonkeyPatch) -> None:
    minted, captured = _connect_with(
        "postgresql://admin@cluster.dsql.eu-west-3.on.aws:5432/postgres",
        monkeypatch,
    )

    assert minted["admin"]["Hostname"] == "cluster.dsql.eu-west-3.on.aws"
    assert captured["password"] == "admin-token"  # noqa: S105 - fake token, not a secret


def test_a_non_admin_role_gets_the_scoped_token_not_the_admin_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Least privilege on the ClickHouse host.

    `generate_db_connect_admin_auth_token` is superuser-equivalent on the whole
    cluster: anything on the analytics box could then read raw member emails
    and workspace ids out of tr_entities -- the exact identifiers
    analytics_surrogate() exists to keep off this host. Deploying the drain as
    a role granted only SELECT/DELETE on the outbox must therefore mint the
    scoped token, or that deployment simply fails to authenticate.
    """
    minted, captured = _connect_with(
        "host=cluster.dsql.eu-west-3.on.aws user=tr_analytics_drain dbname=postgres",
        monkeypatch,
    )

    assert "admin" not in minted
    assert minted["scoped"]["Hostname"] == "cluster.dsql.eu-west-3.on.aws"
    assert captured["password"] == "scoped-token"  # noqa: S105 - fake token, not a secret


def test_both_connect_paths_bound_every_libpq_socket_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon cannot recover from a socket call that never returns."""
    import clickhouse.ingest_operational_outbox_postgres as drain

    _, dsql = _connect_with(
        "host=cluster.dsql.eu-west-3.on.aws user=tr_analytics_drain dbname=postgres",
        monkeypatch,
    )
    plain: dict[str, Any] = {}
    source = drain.PostgresOperationalOutboxSource(dsn="postgresql://db.internal/tr")

    class _FakePsycopg:
        @staticmethod
        def connect(dsn: str, **kwargs: Any) -> str:
            plain.update(dsn=dsn, **kwargs)
            return "connection"

    source._psycopg = _FakePsycopg
    assert source._connect() == "connection"

    expected = {
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 3,
        "tcp_user_timeout": 30_000,
    }
    for captured in (dsql, plain):
        for key, value in expected.items():
            assert captured[key] == value
    assert "options" not in dsql
    assert plain["options"] == "-c statement_timeout=60000"


def test_connection_is_recycled_before_dsqls_one_hour_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clickhouse.ingest_operational_outbox_postgres as drain

    class _Connection:
        closed = False

        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    old = _Connection()
    new = _Connection()
    connects: list[dict[str, Any]] = []
    source = drain.PostgresOperationalOutboxSource(dsn="postgresql://db.internal/tr")
    source._connection = old
    source._connected_at = 100.0

    class _FakePsycopg:
        @staticmethod
        def connect(dsn: str, **kwargs: Any) -> _Connection:
            connects.append({"dsn": dsn, **kwargs})
            return new

    source._psycopg = _FakePsycopg
    monkeypatch.setattr(
        drain.time,
        "monotonic",
        lambda: 100.0 + drain.CONNECTION_MAX_AGE_SECONDS,
    )

    assert source._live_connection() is new
    assert old.close_calls == 1
    assert len(connects) == 1


def test_main_loop_iteration_sends_systemd_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clickhouse.ingest_operational_outbox_postgres as drain

    class _Source:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def oldest_enqueued_at(self) -> None:
            return None

    monkeypatch.setattr(drain, "PostgresOperationalOutboxSource", _Source)
    monkeypatch.setattr(
        drain,
        "clickhouse_targets_from_env",
        lambda env: [SimpleNamespace(describe=lambda: "test@local/tr")],
    )
    monkeypatch.setattr(drain, "build_operational_writer", lambda targets: object())
    monkeypatch.setattr(
        drain,
        "drain_once",
        lambda *args, **kwargs: drain.SweepResult(
            fetched=0,
            inserted=0,
            rows_per_second=0.0,
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["ingest_operational_outbox_postgres", "--dsn", "postgresql://db/tr", "--once"],
    )

    with (
        tempfile.TemporaryDirectory(prefix="tr-notify-", dir="/tmp") as directory,
        socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as receiver,
    ):
        notify_socket = os.path.join(directory, "notify.sock")
        monkeypatch.setenv("NOTIFY_SOCKET", notify_socket)
        try:
            receiver.bind(notify_socket)
        except PermissionError:
            # The managed macOS test sandbox denies bind(AF_UNIX) even in /tmp.
            # CI and normal hosts take the real datagram path above; retain a
            # call-site proof locally instead of skipping the protection.
            messages: list[str] = []
            monkeypatch.setattr(drain, "sd_notify", messages.append)
            assert drain.main() == 0
            assert set(messages) == {"READY=1", "WATCHDOG=1"}
        else:
            receiver.settimeout(1.0)
            assert drain.main() == 0
            messages_bytes = {receiver.recv(128), receiver.recv(128)}
            assert messages_bytes == {b"READY=1", b"WATCHDOG=1"}


def test_sd_notify_translates_systemds_abstract_socket_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clickhouse._sdnotify as notify

    connected: list[str] = []
    sent: list[bytes] = []

    class _Socket:
        def __enter__(self) -> _Socket:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def connect(self, address: str) -> None:
            connected.append(address)

        def sendall(self, payload: bytes) -> None:
            sent.append(payload)

    monkeypatch.setenv("NOTIFY_SOCKET", "@tr-drain")
    monkeypatch.setattr(notify.socket, "socket", lambda *args: _Socket())

    notify.sd_notify("WATCHDOG=1")

    assert connected == ["\0tr-drain"]
    assert sent == [b"WATCHDOG=1"]


def test_busy_sweep_refreshes_watchdog_before_each_shard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import clickhouse.ingest_operational_outbox_postgres as drain

    messages: list[str] = []
    monkeypatch.setattr(drain, "sd_notify", messages.append)

    drain.drain_once(_FakeSource([]), _FakeWriter(), batch_size=10, shard_count=3)

    assert messages == ["WATCHDOG=1"] * 3


def test_a_password_in_the_dsn_is_refused_on_both_connect_paths() -> None:
    """`--dsn` is argv: a password in it is world-readable via ps."""
    from clickhouse.ingest_operational_outbox_postgres import (
        PostgresOperationalOutboxSource,
    )

    with pytest.raises(ValueError, match="must not contain a password"):
        PostgresOperationalOutboxSource(
            dsn="postgresql://tr:secret@db.internal/tr",
            iam_auth="aws-dsql",
        )._connect()

    with pytest.raises(ValueError, match="must not contain a password"):
        PostgresOperationalOutboxSource(dsn="postgresql://tr:secret@db.internal/tr")._connect()


# --------------------------------------------------------------------------
# Sweep: shard agreement and failure containment
# --------------------------------------------------------------------------


def test_the_drain_sweeps_exactly_the_shards_the_writer_hashes_into() -> None:
    """Two constants, one meaning. Disagreement is silent permanent loss.

    A drain sweeping fewer shards than the writer fills never selects and never
    deletes the rows above its count, so they are never delivered -- while the
    lag metric, which only looks at shards it knows about, still reads healthy.
    """
    import clickhouse.ingest_operational_outbox as spanner_drain
    import clickhouse.ingest_operational_outbox_postgres as postgres_drain

    assert postgres_drain.OUTBOX_SHARDS == OPERATIONAL_ANALYTICS_OUTBOX_SHARDS
    assert spanner_drain.OUTBOX_SHARDS == OPERATIONAL_ANALYTICS_OUTBOX_SHARDS


def test_one_poisoned_shard_does_not_stop_delivery_for_the_others() -> None:
    """A single undeliverable row must not wedge the whole outbox.

    `normalise_operational_event` raises on a payload missing an allowlisted
    column -- which happens naturally when a column is added while rows from
    the previous build are still queued. Letting that unwind the sweep would
    stop ACTIVITY delivery over one bad synthetic event, and the process would
    re-read the same row forever.
    """
    healthy = [_row(f"gen-healthy-{index}", shard=index) for index in range(3)]
    writer = _FakeWriter()

    class _PoisonShard(_FakeSource):
        def fetch_shard(self, shard: int, *, limit: int) -> list[OperationalOutboxRow]:
            if shard == 9:
                raise ValueError("synthetic payload missing required fields: region")
            return super().fetch_shard(shard, limit=limit)

    source = _PoisonShard([*healthy, _row("gen-poison", shard=9)])
    result = drain_once(source, writer, batch_size=10)

    assert result.failed_shards == 1
    assert result.inserted == len(healthy)
    # The poisoned row is still queued -- contained, not lost, not deleted.
    assert [row.event_id for row in source.rows] == ["gen-poison"]


def test_a_failing_sweep_is_reported_rather_than_looking_idle() -> None:
    """rows=0 from failure must be distinguishable from rows=0 from an empty queue."""
    row = _row("gen-all-fail", shard=2)

    class _BrokenSource(_FakeSource):
        def fetch_shard(self, shard: int, *, limit: int) -> list[OperationalOutboxRow]:
            raise RuntimeError("connection expired")

    result = drain_once(_BrokenSource([row]), _FakeWriter(), batch_size=10)

    assert result.inserted == 0
    assert result.failed_shards == OPERATIONAL_ANALYTICS_OUTBOX_SHARDS


class TestClickHouseIdentityIsConfigurable:
    """The writer must address the cluster it was pointed at.

    `--database` was a parameter while `--user` was the literal "tr", so the
    writer silently only worked against the GCP cluster. The AWS-EU node
    authenticates as "default" into database "default" (its schema is applied
    unqualified), so a drain aimed at it failed authentication on the first
    insert — AFTER a batch had been read out of the outbox, which is the worst
    possible moment to learn the credentials are wrong.

    Four independent reviews read this code and none caught it, because
    nothing asserted on the command that actually gets executed.
    """

    @staticmethod
    def _capture(monkeypatch: Any) -> list[list[str]]:
        from clickhouse import ingest_operational_outbox as mod

        seen: list[list[str]] = []

        def fake_run(command: list[str], **kwargs: Any) -> Any:
            seen.append(command)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        return seen

    def _event(self) -> Any:
        from clickhouse.ingest_operational_outbox import CanonicalOperationalEvent

        return CanonicalOperationalEvent(
            event_kind="activity",
            row={"generation_id": "gen-1"},
        )

    def test_defaults_preserve_the_gcp_identity(self, monkeypatch: Any) -> None:
        from clickhouse.ingest_operational_outbox import ClickHouseOperationalWriter

        seen = self._capture(monkeypatch)
        ClickHouseOperationalWriter(password="x").insert([self._event()])  # noqa: S106 - test stub

        command = seen[0]
        assert command[command.index("--user") + 1] == "tr"
        assert command[command.index("--database") + 1] == "tr"

    def test_aws_identity_is_honoured(self, monkeypatch: Any) -> None:
        """The regression: this asserted "tr" no matter what was passed."""
        from clickhouse.ingest_operational_outbox import ClickHouseOperationalWriter

        seen = self._capture(monkeypatch)
        ClickHouseOperationalWriter(
            password="x",  # noqa: S106 - test stub
            user="default",
            database="default",
        ).insert([self._event()])

        command = seen[0]
        assert command[command.index("--user") + 1] == "default"
        assert command[command.index("--database") + 1] == "default"

    def test_clickhouse_client_subprocess_uses_a_stable_explicit_cwd(
        self, monkeypatch: Any
    ) -> None:
        """A rotated parent cwd must not prevent clickhouse-client from starting.

        The systemd unit deliberately uses ``/opt/tr-clickhouse`` so ``python
        -m`` can import the flat installed package.  If an installer ever moves
        that directory out from under a running process, each child must still
        start somewhere valid.  Removing ``cwd=`` from the subprocess call is
        the mutation this test guards.
        """
        from clickhouse import ingest_operational_outbox as mod
        from clickhouse.ingest_operational_outbox import ClickHouseOperationalWriter

        calls: list[dict[str, Any]] = []

        def fake_run(_command: list[str], **kwargs: Any) -> Any:
            calls.append(kwargs)
            return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        ClickHouseOperationalWriter(password="x").insert([self._event()])  # noqa: S106

        assert calls[0]["cwd"] == "/"


# --------------------------------------------------------------------------
# The published lag read
# --------------------------------------------------------------------------


def test_oldest_enqueued_at_reads_the_head_of_the_real_table() -> None:
    """Run against the DDL the store applies, not a dict pretending to be one.

    This is the number /status.json publishes as `analytics.drain_lag_seconds`,
    and it is the only thing that would have made the fifteen-day AWS-EU
    outage visible from outside the VPC: the drain that should have alarmed had
    never been installed.
    """
    conn = _real_schema_conn()
    outbox = PostgresOperationalAnalyticsOutbox(lambda operation: operation(conn))
    oldest = dt.datetime(2026, 8, 2, 3, 0, tzinfo=dt.UTC)
    for index, enqueued_at in enumerate(
        [
            dt.datetime(2026, 8, 17, 11, 0, tzinfo=dt.UTC),
            oldest,
            dt.datetime(2026, 8, 10, 0, 0, tzinfo=dt.UTC),
        ]
    ):
        conn.execute(
            _SEED_ROW_SQL,
            (index, ACTIVITY_EVENT_KIND, f"gen-{index}", "{}", enqueued_at),
        )

    assert outbox.oldest_enqueued_at_tx(conn) == oldest


def test_oldest_enqueued_at_is_none_on_a_fully_drained_table() -> None:
    """None means delivered, not unknown -- the caller must not conflate them."""
    conn = _real_schema_conn()
    outbox = PostgresOperationalAnalyticsOutbox(lambda operation: operation(conn))

    assert outbox.oldest_enqueued_at_tx(conn) is None


def test_oldest_enqueued_at_propagates_read_failures_instead_of_returning_none() -> None:
    """A swallowed exception would publish a dead database as a drained queue.

    The bound and the degrade-to-unavailable live in
    `PostgresStore.operational_analytics_outbox_freshness`, which owns the
    connection. This layer must stay loud, or the store cannot tell "the queue
    is empty" from "the read failed".
    """

    class _Exploding:
        def execute(self, *_args: Any, **_kwargs: Any) -> Any:
            raise RuntimeError("dsql: connection refused")

    outbox = PostgresOperationalAnalyticsOutbox(lambda operation: operation(None))

    with pytest.raises(RuntimeError):
        outbox.oldest_enqueued_at_tx(_Exploding())


def test_the_lag_read_takes_a_connection_so_its_caller_can_bound_it() -> None:
    """Why this is a `_tx` method and not a self-transacting one.

    It runs on the public /status.json build path inside an async handler,
    where a blocking read that waits without limit stops the event loop rather
    than one worker thread. The caller therefore has to be able to wrap it in a
    connection with a pool timeout and a `SET LOCAL statement_timeout`, exactly
    as `PostgresStore.readiness_check` does -- and it can only do that if the
    statement runs on a connection the caller owns.
    """
    assert not hasattr(PostgresOperationalAnalyticsOutbox, "oldest_enqueued_at")
    assert hasattr(PostgresOperationalAnalyticsOutbox, "oldest_enqueued_at_tx")


def test_lag_read_is_an_index_seek_and_never_a_count() -> None:
    """The index exists for this statement; count(*) is the expensive question.

    Ordering by enqueued_at without the index is a full scan on every poll,
    worst exactly when a backlog has made it expensive and most worth reading.
    """
    from trusted_router.storage_postgres_operational_analytics_outbox import (
        SELECT_OLDEST_ENQUEUED_AT_SQL,
    )

    assert "ORDER BY enqueued_at LIMIT 1" in SELECT_OLDEST_ENQUEUED_AT_SQL
    assert "count(" not in SELECT_OLDEST_ENQUEUED_AT_SQL.lower()
    assert any(
        "tr_operational_analytics_outbox_enqueued_at_idx" in statement
        for statement in _outbox_schema_statements()
    )


def test_the_drain_and_the_status_page_run_the_identical_lag_statement() -> None:
    """One object, not two literals kept equal by a comment.

    The published `analytics.drain_lag_seconds` is only worth anything because
    it is the same measurement the drain's own `backlog_alarm` fires on. Two
    copies of the statement could drift while both kept returning a plausible
    number, and only their MEANINGS would part company -- a defect with no
    symptom. The identity check, rather than an equality check, is what makes
    re-typing the literal a failure instead of a coincidence away from passing.
    """
    from clickhouse.ingest_operational_outbox_postgres import SELECT_OLDEST_SQL
    from trusted_router.storage_postgres_operational_analytics_outbox import (
        SELECT_OLDEST_ENQUEUED_AT_SQL,
    )

    assert SELECT_OLDEST_SQL is SELECT_OLDEST_ENQUEUED_AT_SQL


def test_dsql_is_not_sent_a_statement_timeout_it_rejects() -> None:
    """Aurora DSQL answers `SET LOCAL statement_timeout` with an ERROR.

    Not "ignores it" -- it raises, which aborts the transaction and takes the
    read with it. The AWS-EU control plane published
    `analytics: {available: false, reason: "unreachable"}` for an outbox it
    could read perfectly well, because the bound added to make that read safe
    was the only thing failing. Verified against the live cluster 2026-08-18:

        ERROR:  setting configuration parameter "statement_timeout" not supported

    So the statement bound is issued only where the backend accepts it, and the
    caller keeps its own (pool-acquire timeout; a caller-side wait on the
    status path). This test pins the decision, not the plumbing.
    """
    from trusted_router.storage_postgres import PostgresStore

    executed: list[str] = []

    class _Conn:
        def execute(self, sql: str, *_a: object, **_k: object) -> object:
            executed.append(sql)
            return _Rows()

    class _Rows:
        def fetchone(self) -> tuple[int, ...]:
            return (1,)

    store = PostgresStore.__new__(PostgresStore)

    store._supports_statement_timeout = False  # DSQL
    store._bound_statement(_Conn(), 3.0)
    assert executed == [], f"DSQL was sent {executed!r}, which it rejects"

    store._supports_statement_timeout = True  # stock Postgres
    store._bound_statement(_Conn(), 3.0)
    assert executed == ["SET LOCAL statement_timeout = '3s'"], executed
