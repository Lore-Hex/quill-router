from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import inspect
import json
import re
import textwrap
import threading
from typing import Any

import httpx
import pytest

from clickhouse.ingest_operational_outbox import (
    _WATERMARK_EPOCH,
    CanonicalOperationalEvent,
    OperationalOutboxRow,
    OutboxSource,
    SpannerOperationalOutboxSource,
    drain_once,
    expand_client_events_payload,
    normalise_operational_event,
)
from clickhouse.rollup_synthetic import (
    build_raw_rollups,
    complete_window_rollups,
    monthly_from_daily,
)
from trusted_router.client_events_schema import ClientEventsBatch
from trusted_router.operational_analytics import (
    OperationalAnalyticsClient,
    stable_rows_fingerprint,
)
from trusted_router.storage_gcp import SpannerBigtableStore
from trusted_router.storage_gcp_operational_analytics_outbox import (
    SpannerOperationalAnalyticsOutbox,
    activity_payload,
    analytics_surrogate,
    operational_analytics_shard,
)
from trusted_router.storage_models import (
    Generation,
    ProviderBenchmarkSample,
    SyntheticProbeSample,
)
from trusted_router.storage_operational_analytics import (
    CLIENT_EVENTS_EVENT_KIND,
    OPERATIONAL_ANALYTICS_OUTBOX_SHARDS,
    build_client_events_payload,
)
from trusted_router.storage_postgres import PostgresStore
from trusted_router.storage_postgres_operational_analytics_outbox import (
    PostgresOperationalAnalyticsOutbox,
)
from trusted_router.types import UsageType


def _generation() -> Generation:
    return Generation(
        id="gen-activity-1",
        request_id="req-activity-1",
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


class _ParamTypes:
    INT64 = "INT64"
    STRING = "STRING"
    TIMESTAMP = "TIMESTAMP"

    @staticmethod
    def Array(element: str) -> str:  # noqa: N802 - mirrors google.cloud.spanner_v1.param_types
        return f"ARRAY<{element}>"


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


def test_activity_payload_names_its_workspace_but_never_keys_or_content() -> None:
    """The 2026-08-19 boundary: workspace_id is pseudonymous and belongs in the
    row (it is what makes rows joinable without a refreshed directory); key
    hashes and generation content remain surrogate-only/absent. If this test
    fails on the workspace_id line, someone is re-anonymising the private
    table -- that decision was made deliberately, reverse it deliberately."""
    generation = _generation()
    payload = activity_payload(generation)
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["tenant_id"] == analytics_surrogate("workspace", generation.workspace_id)
    assert payload["workspace_id"] == generation.workspace_id
    assert payload["key_id"] == analytics_surrogate("api-key", generation.key_hash)
    assert generation.key_hash not in encoded
    assert "private model output" not in encoded
    assert "tool_calls" not in payload
    assert "operator_cost_microdollars" not in payload
    assert "prompt_content" not in payload
    assert "output_content" not in payload


def test_operational_outbox_enqueue_is_sharded_and_commit_timestamped() -> None:
    database = _Database()
    generation = _generation()
    SpannerOperationalAnalyticsOutbox(database, _ParamTypes()).enqueue_activity(generation)

    [(sql, params, param_types)] = database.transaction.calls
    assert "PENDING_COMMIT_TIMESTAMP()" in sql
    assert params["event_kind"] == "activity"
    assert params["event_id"] == generation.id
    assert params["shard"] == operational_analytics_shard(f"activity:{generation.id}")
    assert json.loads(params["payload"])["tenant_id"] != generation.workspace_id
    assert param_types == {
        "shard": "INT64",
        "event_kind": "STRING",
        "event_id": "STRING",
        "payload": "STRING",
    }


def _client_events_payload() -> dict[str, Any]:
    batch = ClientEventsBatch.model_validate(
        {
            "schema_version": 1,
            "batch_id": "a" * 32,
            "instance_id": "b" * 16,
            "seq": 9,
            "sent_at_ms": 0,
            "sdk": {
                "name": "tr-py",
                "version": "1.2.3",
                "lang": "python",
                "runtime": "cpython/3.12.4",
                "os": "linux",
                "arch": "arm64",
            },
            "events": [
                {
                    "age_ms": 1_500,
                    "plane": "inference",
                    "endpoint": "responses",
                    "method": "POST",
                    "streaming": True,
                    "provider_pinned": False,
                    "model": "private/model-name",
                    "attempts": [
                        {
                            "index": 0,
                            "host": "ally",
                            "outcome": "transport_error",
                            "http_status": None,
                            "error_class": "connect_timeout",
                            "error_source": "router",
                            "should_retry": "false",
                            "retry_after_ms": None,
                            "elapsed_ms": 10_000,
                            "ttfb_ms": None,
                            "request_id": None,
                            "moved": False,
                        }
                    ],
                    "final_outcome": "exhausted",
                    "final_http_status": None,
                    "total_ms": 10_000,
                    "ttft_ms": None,
                    "failover_used": False,
                    "timeout_phase": "connect",
                    "configured_timeout_ms": 10_000,
                    "sample_rate": 1.0,
                    "sample_reason": "failure",
                }
            ],
            "counters": [
                {
                    "window_start_age_ms": 61_500,
                    "level": "request",
                    "endpoint": "responses",
                    "streaming": True,
                    "host": "apex",
                    "outcome": "ok",
                    "error_class": None,
                    "http_status_class": "2xx",
                    "timeout_phase": "none",
                    "timeout_floor_met": False,
                    "provider_pinned": False,
                    "requests": 4,
                    "attempts": 4,
                    "failover_used": 0,
                    "first_attempt_success": 4,
                    "total_ms_hist": {"lt400": 4},
                    "first_event_ms_hist": {"lt200": 4},
                }
            ],
        }
    )
    return build_client_events_payload(
        batch,
        tenant_id="raw-workspace",
        key_id="raw-key-hash",
        received_at=dt.datetime(2026, 8, 17, 12, 1, 2, 345000, tzinfo=dt.UTC),
        is_synthetic=False,
        success_sample_rate=0.01,
    )


def test_client_events_payload_round_trips_through_clickhouse_expansion() -> None:
    payload = _client_events_payload()

    rows = expand_client_events_payload(
        payload,
        dt.datetime(2026, 8, 17, 12, 1, 3, tzinfo=dt.UTC),
    )

    assert set(payload) == {
        "schema_version",
        "tenant_id",
        "key_id",
        "received_at",
        "clock_skew_ms",
        "synthetic",
        "batch_id",
        "instance_id",
        "seq",
        "sdk",
        "events",
        "counters",
    }
    assert len(rows) == 2
    request = next(row.row for row in rows if row.event_kind == "client_request")
    counter = next(row.row for row in rows if row.event_kind == "client_counter")
    assert request["tr_fault"] == 1
    assert counter["tr_fault"] == 0
    assert request["final_host"] == "ally"
    assert request["model"] == "other"


def test_spanner_client_events_enqueue_uses_one_batch_outbox_row() -> None:
    database = _Database()
    payload = _client_events_payload()

    SpannerOperationalAnalyticsOutbox(database, _ParamTypes()).enqueue_client_events(payload)

    [(sql, params, _)] = database.transaction.calls
    event_id = f"{payload['tenant_id']}:{payload['batch_id']}"
    assert "PENDING_COMMIT_TIMESTAMP()" in sql
    assert params["event_kind"] == CLIENT_EVENTS_EVENT_KIND
    assert params["event_id"] == event_id
    assert params["shard"] == operational_analytics_shard(f"{CLIENT_EVENTS_EVENT_KIND}:{event_id}")
    assert json.loads(params["payload"]) == payload


def test_postgres_client_events_enqueue_uses_one_idempotent_batch_row() -> None:
    statements: list[tuple[str, tuple[Any, ...]]] = []

    class Connection:
        def execute(self, sql: str, params: tuple[Any, ...]) -> None:
            statements.append((sql, params))

    connection = Connection()
    outbox = PostgresOperationalAnalyticsOutbox(lambda operation: operation(connection))
    payload = _client_events_payload()

    outbox.enqueue_client_events(payload)
    outbox.enqueue_client_events(payload)

    assert len(statements) == 2
    for sql, params in statements:
        event_id = f"{payload['tenant_id']}:{payload['batch_id']}"
        assert "ON CONFLICT" in sql
        assert params[1] == CLIENT_EVENTS_EVENT_KIND
        assert params[2] == event_id
        assert json.loads(params[3]) == payload


def test_clickhouse_balanced_benchmark_reader_uses_one_window_query() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["param_per_provider_limit"] == "25"
        assert request.url.params["param_limit"] == "5000"
        assert request.url.params["param_cutoff"] == "2026-07-31T00:00:00Z"
        sql = request.content.decode()
        assert "row_number() OVER" in sql
        assert "PARTITION BY provider" in sql
        assert "provider_rank <= {per_provider_limit:UInt32}" in sql
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "bench-balanced-1",
                        "model": "anthropic/claude-haiku-4.5",
                        "provider": "anthropic",
                        "provider_name": "Anthropic",
                        "status": "success",
                        "usage_type": "Credits",
                        "streamed": 1,
                        "input_tokens": 10,
                        "output_tokens": 2,
                        "total_cost_microdollars": 7,
                        "speed_tokens_per_second": 8.0,
                        "elapsed_milliseconds": 250,
                        "first_token_milliseconds": 100,
                        "ttfb_milliseconds": 20,
                        "finish_reason": "stop",
                        "error_type": None,
                        "error_status": None,
                        "error_message": None,
                        "region": "us-central1",
                        "source": "synthetic",
                        "app": "TrustedRouter Synthetic",
                        "created_at": "2026-07-31 12:34:56.789",
                    }
                ]
            },
        )

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse",
        user="reader",
        password="sec" + "ret",
        transport=httpx.MockTransport(handler),
    )
    rows = client.balanced_benchmark_samples(
        cutoff="2026-07-31T00:00:00Z",
        per_provider_limit=25,
        limit=5000,
    )

    assert calls == 1
    assert rows == [
        ProviderBenchmarkSample(
            id="bench-balanced-1",
            model="anthropic/claude-haiku-4.5",
            provider="anthropic",
            provider_name="Anthropic",
            status="success",
            usage_type="Credits",
            streamed=True,
            input_tokens=10,
            output_tokens=2,
            total_cost_microdollars=7,
            speed_tokens_per_second=8.0,
            elapsed_milliseconds=250,
            first_token_milliseconds=100,
            ttfb_milliseconds=20,
            finish_reason="stop",
            region="us-central1",
            source="synthetic",
            app="TrustedRouter Synthetic",
            created_at="2026-07-31T12:34:56.789Z",
        )
    ]


def test_clickhouse_route_benchmark_reader_uses_one_partitioned_query() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert request.url.params["param_per_route_limit"] == "48"
        assert request.url.params["param_limit"] == "47088"
        assert request.url.params["param_cutoff"] == "2026-08-29T00:00:00Z"
        sql = request.content.decode()
        assert "source = 'synthetic'" in sql
        assert "PARTITION BY provider, model" in sql
        assert "route_rank <= {per_route_limit:UInt32}" in sql
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "bench-route-1",
                        "model": "openai/gpt-5.5",
                        "provider": "openai",
                        "provider_name": "OpenAI",
                        "status": "success",
                        "usage_type": "Credits",
                        "streamed": 1,
                        "input_tokens": 4,
                        "output_tokens": 1,
                        "total_cost_microdollars": 3,
                        "speed_tokens_per_second": 9.0,
                        "elapsed_milliseconds": 180,
                        "first_token_milliseconds": 90,
                        "ttfb_milliseconds": 15,
                        "finish_reason": "stop",
                        "error_type": None,
                        "error_status": None,
                        "error_message": None,
                        "region": "us-central1",
                        "source": "synthetic",
                        "app": "TrustedRouter Synthetic",
                        "created_at": "2026-08-30 12:34:56.789",
                    }
                ]
            },
        )

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse",
        user="reader",
        password="sec" + "ret",
        transport=httpx.MockTransport(handler),
    )
    rows = client.route_benchmark_samples(
        cutoff="2026-08-29T00:00:00Z",
        per_route_limit=48,
        limit=47_088,
    )

    assert calls == 1
    assert [(row.provider, row.model, row.id) for row in rows] == [
        ("openai", "openai/gpt-5.5", "bench-route-1")
    ]


def test_gcp_route_health_batch_read_does_not_shadow_to_bigtable() -> None:
    class FakeAnalytics:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        def route_benchmark_samples(
            self,
            *,
            cutoff: str,
            per_route_limit: int,
            limit: int,
        ) -> list[ProviderBenchmarkSample]:
            self.calls.append(
                {
                    "cutoff": cutoff,
                    "per_route_limit": per_route_limit,
                    "limit": limit,
                }
            )
            return []

    store = object.__new__(SpannerBigtableStore)
    analytics = FakeAnalytics()
    store._operational_analytics = analytics  # type: ignore[assignment]

    rows = store.provider_route_benchmark_samples(
        cutoff="2026-08-29T00:00:00Z",
        per_route_limit=48,
        limit=47_088,
    )

    assert rows == []
    assert analytics.calls == [
        {
            "cutoff": "2026-08-29T00:00:00Z",
            "per_route_limit": 48,
            "limit": 47_088,
        }
    ]


def test_postgres_route_health_batch_read_uses_one_partitioned_query() -> None:
    statements: list[tuple[str, tuple[object, ...]]] = []
    body = dataclasses.asdict(
        ProviderBenchmarkSample(
            id="bench-postgres-route-1",
            model="openai/gpt-5.5",
            provider="openai",
            provider_name="OpenAI",
            status="success",
            usage_type="Credits",
            streamed=True,
            input_tokens=4,
            output_tokens=1,
            total_cost_microdollars=3,
            created_at="2026-08-30T12:34:56.789Z",
            source="synthetic",
        )
    )

    class Cursor:
        def fetchall(self) -> list[tuple[dict[str, object]]]:
            return [(body,)]

    class Connection:
        def execute(self, sql: str, params: tuple[object, ...]) -> Cursor:
            statements.append((sql, params))
            return Cursor()

    connection = Connection()
    store = PostgresStore.__new__(PostgresStore)
    store._run_transaction = lambda operation: operation(connection)  # type: ignore[method-assign]

    rows = store.provider_route_benchmark_samples(
        cutoff="2026-08-29T00:00:00Z",
        per_route_limit=48,
        limit=47_088,
    )

    assert [(row.provider, row.model, row.id) for row in rows] == [
        ("openai", "openai/gpt-5.5", "bench-postgres-route-1")
    ]
    [(sql, params)] = statements
    assert "PARTITION BY body ->> 'provider', body ->> 'model'" in sql
    assert "body ->> 'source' = 'synthetic'" in sql
    assert "route_rank <= %s" in sql
    assert params == ("2026-08-29T00:00:00Z", 48, 47_088)


@pytest.mark.parametrize(
    "snapshot_name",
    [
        "leaderboard",
        "apps",
        "video_leaderboard",
        "status_inputs",
        "client_reliability",
    ],
)
def test_public_snapshot_reads_newest_revision_across_month_partitions(
    snapshot_name: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        sql = request.content.decode()
        assert "WHERE name = {name:String}" in sql
        assert "ORDER BY generated_at DESC" in sql
        assert request.url.params["param_name"] == snapshot_name
        return httpx.Response(
            200,
            json={"data": [{"payload": '{"generated_at":"2026-08-01T00:00:00Z"}'}]},
        )

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse",
        user="reader",
        password="sec" + "ret",
        transport=httpx.MockTransport(handler),
    )

    assert client.public_snapshot(snapshot_name) == {"generated_at": "2026-08-01T00:00:00Z"}


def test_public_snapshot_rejects_unknown_products_without_querying() -> None:
    client = OperationalAnalyticsClient(
        base_url="http://clickhouse",
        user="reader",
        password="secret",  # noqa: S106 - inert test credential.
    )

    with pytest.raises(ValueError, match="unsupported public analytics snapshot"):
        client.public_snapshot("prompt_contents")


def test_public_snapshot_uses_a_short_optional_read_timeout(monkeypatch) -> None:
    client = OperationalAnalyticsClient(
        base_url="http://clickhouse",
        user="reader",
        password="secret",  # noqa: S106 - inert test credential.
    )
    observed: dict[str, float] = {}

    def query(_sql, *, params=None, timeout_seconds=20.0):
        _ = params
        observed["timeout_seconds"] = timeout_seconds
        return []

    monkeypatch.setattr(client, "_query", query)

    assert client.public_snapshot("leaderboard") is None
    assert observed == {"timeout_seconds": 2.0}


def _outbox_row() -> OperationalOutboxRow:
    return OperationalOutboxRow(
        shard=2,
        commit_ts=dt.datetime(2026, 7, 31, 12, 35, tzinfo=dt.UTC),
        event_kind="activity",
        event_id=_generation().id,
        payload=json.dumps(activity_payload(_generation())),
    )


def test_operational_normalizer_adds_commit_version_and_rejects_unknown_kind() -> None:
    [event] = normalise_operational_event(_outbox_row())
    assert event.event_kind == "activity"
    assert event.row["generation_id"] == _generation().id
    assert event.row["ingest_version"].startswith("2026-07-31T12:35:00")

    with pytest.raises(ValueError, match="unsupported operational event kind"):
        normalise_operational_event(dataclasses.replace(_outbox_row(), event_kind="prompt"))


class _Source:
    def __init__(self, rows: list[OperationalOutboxRow]) -> None:
        self.rows = rows
        self.deleted: list[OperationalOutboxRow] = []

    def fetch(self, *, limit: int) -> list[OperationalOutboxRow]:
        return self.rows[:limit]

    def delete(self, rows: list[OperationalOutboxRow]) -> None:
        self.deleted.extend(rows)
        self.rows = [row for row in self.rows if row not in rows]

    def oldest_commit_ts(self) -> dt.datetime | None:
        return self.rows[0].commit_ts if self.rows else None


class _Writer:
    def __init__(self, *, failures: int = 0) -> None:
        self.failures = failures
        self.batches: list[list[CanonicalOperationalEvent]] = []

    def insert(self, events: list[CanonicalOperationalEvent]) -> None:
        self.batches.append(events)
        if self.failures:
            self.failures -= 1
            raise RuntimeError("ClickHouse unavailable")


def test_outbox_source_protocol_is_behavior_free() -> None:
    """Protocols define the drain contract; implementations own all behavior."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(OutboxSource)))
    methods = [node for node in tree.body[0].body if isinstance(node, ast.FunctionDef)]

    assert methods
    for method in methods:
        assert len(method.body) == 1, f"OutboxSource.{method.name} carries behavior"
        [statement] = method.body
        assert (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and statement.value.value is Ellipsis
        ), f"OutboxSource.{method.name} carries behavior"


class _QueryCountingSnapshot:
    def __init__(self, database: _QueryCountingDatabase) -> None:
        self.database = database

    def __enter__(self) -> _QueryCountingSnapshot:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def execute_sql(
        self,
        sql: str,
        *,
        params: dict[str, Any],
        param_types: dict[str, Any],
    ) -> list[tuple[object, ...]]:
        self.database.select_statements.append(sql)
        self.database.select_params.append((params, param_types))
        if not self.database.pending_rows:
            return []
        batch = self.database.pending_rows.pop(0)
        return [
            (row.shard, row.commit_ts, row.event_kind, row.event_id, row.payload) for row in batch
        ]


class _QueryCountingBatch:
    def __init__(self, database: _QueryCountingDatabase) -> None:
        self.database = database

    def __enter__(self) -> _QueryCountingBatch:
        return self

    def __exit__(self, *args: object) -> None:
        pass

    def delete(self, table: str, key_set: object) -> None:
        self.database.delete_calls.append((table, key_set))


class _QueryCountingDatabase:
    def __init__(self, rows: list[list[OperationalOutboxRow]] | None = None) -> None:
        self.select_statements: list[str] = []
        self.select_params: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.delete_calls: list[tuple[str, object]] = []
        # Each fetch pops one batch; an exhausted queue means an empty outbox.
        self.pending_rows: list[list[OperationalOutboxRow]] = (
            [[_outbox_row()]] if rows is None else rows
        )

    def snapshot(self, **_kwargs: object) -> _QueryCountingSnapshot:
        return _QueryCountingSnapshot(self)

    def batch(self) -> _QueryCountingBatch:
        return _QueryCountingBatch(self)


def _spanner_source(database: _QueryCountingDatabase) -> SpannerOperationalOutboxSource:
    source = object.__new__(SpannerOperationalOutboxSource)
    source._database = database
    source._pt = _ParamTypes()
    source._shard_count = 32
    source._after = None
    return source


def test_spanner_drain_fetch_is_one_seek_statement_with_a_watermark() -> None:
    """One statement per poll, seeking every shard from the watermark.

    The unfiltered ``LIMIT @limit`` this replaces walked the table's deleted-row
    garbage on every poll: 0.207s CPU per execution for ~2 rows, 3,300/hour --
    the largest load on the billing-plane instance (SPANNER_SYS, 2026-09-01).
    The per-shard predicate is what makes the read a set of key-range seeks;
    a bare ``commit_ts >= @after`` is not a key prefix and would still scan.
    """
    database = _QueryCountingDatabase()
    source = _spanner_source(database)

    result = drain_once(source, _Writer(), batch_size=100)

    assert result.fetched == 1
    assert len(database.select_statements) == 1
    statement = database.select_statements[0]
    assert "WHERE shard IN UNNEST(@shards)" in statement
    assert "commit_ts >= @after" in statement
    assert "ORDER BY commit_ts LIMIT @limit" in statement
    params, types = database.select_params[0]
    assert params == {"shards": list(range(32)), "after": _WATERMARK_EPOCH, "limit": 100}
    assert types == {"shards": "ARRAY<INT64>", "after": "TIMESTAMP", "limit": "INT64"}
    assert len(database.delete_calls) == 1


def test_spanner_drain_watermark_advances_only_after_clickhouse_ack() -> None:
    """The watermark moves to the newest DELETED commit_ts, never to a fetched one.

    A batch whose insert failed is never deleted, so the next poll must seek
    from the old watermark and see it again; after an acknowledged batch the
    next poll seeks from that batch's newest commit timestamp (``>=`` keeps
    tie-sharing rows that LIMIT may have cut).
    """
    row = _outbox_row()
    database = _QueryCountingDatabase(rows=[[row], [row], []])
    source = _spanner_source(database)

    with pytest.raises(RuntimeError, match="ClickHouse unavailable"):
        drain_once(source, _Writer(failures=1), batch_size=100)
    assert database.delete_calls == []
    assert source._after is None

    acked = drain_once(source, _Writer(), batch_size=100)
    assert acked.fetched == 1
    assert len(database.delete_calls) == 1
    assert source._after == row.commit_ts

    drain_once(source, _Writer(), batch_size=100)
    assert database.select_params[-1][0]["after"] == row.commit_ts
    assert database.select_params[0][0]["after"] == _WATERMARK_EPOCH


def test_operational_cursor_advances_only_after_clickhouse_ack() -> None:
    row = _outbox_row()
    source = _Source([row])
    writer = _Writer(failures=1)

    with pytest.raises(RuntimeError, match="ClickHouse unavailable"):
        drain_once(source, writer, batch_size=10)
    assert source.rows == [row]
    assert source.deleted == []

    result = drain_once(source, writer, batch_size=10)
    assert result.inserted == 1
    assert source.rows == []
    assert source.deleted == [row]
    assert len(writer.batches) == 2


def test_clickhouse_activity_reader_binds_private_filters_and_never_sends_raw_ids() -> None:
    tenant_id = analytics_surrogate("workspace", "ws-private-123")
    key_id = analytics_surrogate("api-key", "key-hash")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["param_tenant_id"] == tenant_id
        assert request.url.params["param_key_id"] == key_id
        assert request.url.params["param_tag_key"] == "team"
        assert request.url.params["param_tag_value"] == "legal"
        body = request.content.decode()
        assert "ws-private-123" not in body
        assert "key-hash" not in body
        assert "{tenant_id:String}" in body
        assert "mapContains(tags, {tag_key:String})" in body
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "generation_id": "gen-1",
                        "request_id": "req-1",
                        "key_id": analytics_surrogate("api-key", "key-hash"),
                        "model": "anthropic/claude-haiku-4.5",
                        "provider": "anthropic",
                        "provider_name": "Anthropic",
                        "app": "Test",
                        "tokens_prompt": 2,
                        "tokens_completion": 1,
                        "cached_input_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_cost_microdollars": 1,
                        "usage_type": "Credits",
                        "speed_tokens_per_second": 4.0,
                        "finish_reason": "stop",
                        "status": "success",
                        "streamed": 1,
                        "usage_estimated": 0,
                        "elapsed_milliseconds": 250,
                        "first_token_milliseconds": 100,
                        "ttfb_milliseconds": 20,
                        "region": "us-central1",
                        "user": None,
                        "session_id": None,
                        "http_referer": None,
                        "app_categories": [],
                        "tags": {"team": "legal"},
                        "created_at": "2026-07-31 12:34:56.789",
                    }
                ]
            },
        )

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123",
        user="reader",
        password="secret",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(handler),
    )
    [generation] = client.activity_generations(
        tenant_id=tenant_id,
        key_id=key_id,
        tag_key="team",
        tag_value="legal",
        limit=10,
    )
    assert generation.workspace_id == tenant_id
    assert generation.tags == {"team": "legal"}
    assert generation.created_at == "2026-07-31T12:34:56.789Z"


def test_client_reliability_reader_binds_tenant_and_uses_final_rollups() -> None:
    tenant_id = analytics_surrogate("workspace", "ws-client-reliability")

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert request.url.params["param_tenant_id"] == tenant_id
        assert request.url.params["param_window_minutes"] == "60"
        assert "ws-client-reliability" not in body
        assert "FROM client_availability_rollups FINAL" in body
        assert "tenant_id = {tenant_id:String}" in body
        assert "period IN ('5m', 'hour')" in body
        assert "INTERVAL {window_minutes:UInt32} MINUTE" in body
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "period": "5m",
                        "host": "",
                        "endpoint": "",
                        "sdk": "",
                        "requests": 100,
                        "successes": 99,
                        "tr_fault_failures": 1,
                        "excluded_failures": 2,
                        "aborted": 1,
                        "attempts": 110,
                        "attempt_tr_fault": 0,
                        "failover_used": 5,
                        "first_attempt_success": 94,
                        "total_ms_hist": {"lt100": 50, "lt200": 50},
                        "first_event_ms_hist": {"lt100": 100},
                    },
                    {
                        "period": "5m",
                        "host": "apex",
                        "endpoint": "",
                        "sdk": "",
                        "requests": 0,
                        "successes": 0,
                        "tr_fault_failures": 0,
                        "excluded_failures": 0,
                        "aborted": 0,
                        "attempts": 110,
                        "attempt_tr_fault": 2,
                        "failover_used": 0,
                        "first_attempt_success": 0,
                        "total_ms_hist": {},
                        "first_event_ms_hist": {},
                    },
                ]
            },
        )

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123",
        user="reader",
        password="secret",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(handler),
    )

    summary = client.client_reliability_summary(tenant_id, window_minutes=60)

    assert summary == {
        "requests": 100,
        "successes": 99,
        "tr_fault": 1,
        "excluded": 2,
        "aborted": 1,
        "attempts": 110,
        "failover_used": 5,
        "first_attempt_success": 94,
        "p50_total_ms": 100,
        "p95_total_ms": 200,
        "p50_ttft_ms": 100,
        "by_host": {"apex": {"attempts": 110, "attempt_tr_fault": 2, "rate": 0.018182}},
    }


def test_client_event_reader_binds_since_limit_and_normalizes_failures() -> None:
    tenant_id = analytics_surrogate("workspace", "ws-client-events")
    since = dt.datetime(2026, 8, 17, 10, 30, tzinfo=dt.UTC)

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        assert request.url.params["param_tenant_id"] == tenant_id
        assert request.url.params["param_since"] == "2026-08-17T10:30:00Z"
        assert request.url.params["param_limit"] == "50"
        assert "ws-client-events" not in body
        assert "FROM client_request_events FINAL" in body
        assert "created_at >= parseDateTime64BestEffort({since:String}, 3)" in body
        assert "final_outcome != 'ok'" in body
        assert "LIMIT {limit:UInt32}" in body
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "created_at": "2026-08-17 10:45:00.000",
                        "endpoint": "responses",
                        "model": "openai/gpt-5",
                        "attempt_host": ["apex", "ally"],
                        "attempt_count": 2,
                        "final_outcome": "transport_error",
                        "final_http_status": 0,
                        "first_error_class": "connect_timeout",
                        "sdk": "tr-py",
                        "sdk_version": "0.6.0",
                        "attempt_request_id": ["", "rlog_0123456789abcdef0123456789abcdef"],
                    }
                ]
            },
        )

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123",
        user="reader",
        password="secret",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(handler),
    )

    rows = client.client_events_recent(tenant_id, since=since, limit=100)

    assert rows == [
        {
            "created_at": "2026-08-17T10:45:00.000Z",
            "endpoint": "responses",
            "model": "openai/gpt-5",
            "attempt_host": ["apex", "ally"],
            "attempt_count": 2,
            "final_outcome": "transport_error",
            "final_http_status": None,
            "first_error_class": "connect_timeout",
            "sdk": "tr-py",
            "sdk_version": "0.6.0",
            "attempt_request_id": ["rlog_0123456789abcdef0123456789abcdef"],
        }
    ]


def _read_router(mode: str) -> SpannerBigtableStore:
    store = object.__new__(SpannerBigtableStore)
    store._analytics_read_mode = mode
    store._analytics_dual_read_grace_seconds = 0
    store._analytics_parity_log_lock = threading.Lock()
    store._analytics_last_parity_log = {}
    return store


def test_dual_read_returns_bigtable_and_tolerates_clickhouse_failure() -> None:
    store = _read_router("dual")
    result = store._analytics_read(
        "test",
        bigtable=lambda: ["bigtable"],
        clickhouse=lambda: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert result == ["bigtable"]


def test_clickhouse_primary_falls_back_to_bigtable() -> None:
    store = _read_router("clickhouse")
    result = store._analytics_read(
        "test",
        bigtable=lambda: ["fallback"],
        clickhouse=lambda: (_ for _ in ()).throw(RuntimeError("down")),
    )
    assert result == ["fallback"]


def test_parity_fingerprint_ignores_rebuild_time_opaque_ids_and_order() -> None:
    first = [
        {
            "id": "one",
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
            "workspace_id": "raw-workspace",
            "key_hash": "raw-key-hash",
            "requests": 2,
        },
        {"id": "two", "created_at": "2020-01-02T00:00:00Z", "requests": 3},
    ]
    second = [
        {"id": "two", "created_at": "2020-01-02T00:00:00Z", "requests": 3},
        {
            "id": "one",
            "created_at": "2020-01-01T00:00:00Z",
            "updated_at": "2026-07-31T00:00:00Z",
            "workspace_id": analytics_surrogate("workspace", "raw-workspace"),
            "key_hash": analytics_surrogate("api-key", "raw-key-hash"),
            "requests": 2,
        },
    ]
    assert stable_rows_fingerprint(first, grace_seconds=0) == stable_rows_fingerprint(
        second,
        grace_seconds=0,
    )


def test_parity_fingerprint_matches_clickhouse_float32_benchmark_storage() -> None:
    high_precision = [
        {
            "id": "bench-one",
            "created_at": "2020-01-01T00:00:00Z",
            "input_tokens": 1,
            "speed_tokens_per_second": 1.234567890123,
        }
    ]
    stored_float32 = [
        {
            "id": "bench-one",
            "created_at": "2020-01-01T00:00:00Z",
            "input_tokens": 1,
            "speed_tokens_per_second": 1.2345678806304932,
        }
    ]
    assert stable_rows_fingerprint(
        high_precision,
        grace_seconds=0,
    ) == stable_rows_fingerprint(stored_float32, grace_seconds=0)


def _synthetic_sample(
    sample_id: str,
    *,
    status: str,
    created_at: str,
    latency: int,
    error_type: str | None = None,
) -> SyntheticProbeSample:
    return SyntheticProbeSample(
        id=sample_id,
        probe_type="tls_health",
        target="regional_api",
        target_url="https://api-us-central1.quillrouter.com/health",
        monitor_region="us-central1",
        target_region="us-central1",
        status=status,
        latency_milliseconds=latency,
        ttfb_milliseconds=latency - 1,
        error_type=error_type,
        cost_microdollars=2,
        created_at=created_at,
    )


def test_synthetic_rollups_preserve_exact_counts_histograms_and_costs() -> None:
    samples = [
        _synthetic_sample(
            "sample-up",
            status="up",
            created_at="2026-07-31T12:01:00Z",
            latency=10,
        ),
        _synthetic_sample(
            "sample-down",
            status="down",
            created_at="2026-07-31T12:02:00Z",
            latency=30,
            error_type="timeout",
        ),
    ]

    fixed_now = "2026-07-31T12:03:00Z"
    first = build_raw_rollups(samples, periods={"hour", "day"}, now=lambda: fixed_now)
    second = build_raw_rollups(samples, periods={"hour", "day"}, now=lambda: fixed_now)
    assert [dataclasses.asdict(item) for item in first] == [
        dataclasses.asdict(item) for item in second
    ]

    hourly = [item for item in first if item.period == "hour"]
    assert hourly
    for rollup in hourly:
        assert rollup.sample_count == 2
        assert rollup.up_count == 1
        assert rollup.down_count == 1
        assert rollup.latency_histogram == {"10": 1, "30": 1}
        assert rollup.error_counts == {"timeout": 1}
        assert rollup.cost_microdollars == 4


def test_synthetic_rollups_count_repeated_sample_id_once() -> None:
    first = _synthetic_sample(
        "shared-heartbeat",
        status="down",
        created_at="2026-07-31T12:01:00Z",
        latency=30,
        error_type="timeout",
    )
    latest = _synthetic_sample(
        "shared-heartbeat",
        status="up",
        created_at="2026-07-31T12:04:00Z",
        latency=10,
    )

    rollups = build_raw_rollups([first, latest], periods={"hour", "day"})

    assert rollups
    for rollup in rollups:
        assert rollup.sample_count == 1
        assert rollup.up_count == 1
        assert rollup.down_count == 0
        assert rollup.latency_histogram == {"10": 1}
        assert rollup.error_counts == {}
        assert rollup.cost_microdollars == 2


def test_synthetic_monthly_rollups_merge_daily_without_losing_dimensions() -> None:
    day_one = build_raw_rollups(
        [
            _synthetic_sample(
                "sample-one",
                status="up",
                created_at="2026-07-01T12:01:00Z",
                latency=10,
            )
        ],
        periods={"day"},
    )
    day_two = build_raw_rollups(
        [
            _synthetic_sample(
                "sample-two",
                status="degraded",
                created_at="2026-07-02T12:01:00Z",
                latency=20,
                error_type="slow",
            )
        ],
        periods={"day"},
    )

    monthly = monthly_from_daily(day_one + day_two)
    assert monthly
    for rollup in monthly:
        assert rollup.period == "month"
        assert rollup.period_start == "2026-07-01T00:00:00Z"
        assert rollup.sample_count == 2
        assert rollup.up_count == 1
        assert rollup.degraded_count == 1
        assert rollup.latency_histogram == {"10": 1, "20": 1}
        assert rollup.error_counts == {"slow": 1}
        assert rollup.cost_microdollars == 4


def test_synthetic_rollup_rebuild_never_overwrites_partial_ttl_boundary() -> None:
    samples = [
        _synthetic_sample(
            "boundary",
            status="up",
            created_at="2026-07-17T12:45:00Z",
            latency=10,
        ),
        _synthetic_sample(
            "complete",
            status="up",
            created_at="2026-07-18T00:01:00Z",
            latency=20,
        ),
    ]
    rollups = complete_window_rollups(
        build_raw_rollups(samples, periods={"hour", "day"}),
        raw_start=dt.datetime(2026, 7, 17, 12, 30, tzinfo=dt.UTC),
    )
    starts = {(rollup.period, rollup.period_start) for rollup in rollups}
    assert ("hour", "2026-07-17T12:00:00Z") not in starts
    assert ("day", "2026-07-17T00:00:00Z") not in starts
    assert ("day", "2026-07-18T00:00:00Z") in starts


# ---------------------------------------------------------------------------
# The published lag read: how old is the oldest row the drain has not moved?
# ---------------------------------------------------------------------------


class _SnapshotDatabase:
    """A Spanner database whose snapshot answers the one-statement lag read.

    The read is a single `SELECT MIN(commit_ts) FROM (<32 per-shard seeks>)`,
    so this fake parses the shard literals back out of the SQL and answers the
    minimum over the shards it was asked about. Parsing rather than ignoring
    them is deliberate: it is what lets the tests below assert that all 32
    heads really were consulted, which is the property the old 32-round-trip
    form made obvious and this one does not.
    """

    def __init__(self, rows_by_shard: dict[int, list[dt.datetime]]) -> None:
        self._rows_by_shard = rows_by_shard
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.multi_use: list[bool] = []
        self.timeouts: list[float | None] = []

    def snapshot(self, **kwargs: Any) -> Any:
        self.multi_use.append(bool(kwargs.get("multi_use")))
        outer = self

        class _Snapshot:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_exc: Any) -> None:
                return None

            def execute_sql(self, sql: str, **kwargs: Any) -> list[list[Any]]:
                outer.timeouts.append(kwargs.get("timeout"))
                outer.queries.append((sql, kwargs.get("params") or {}))
                shards = [int(value) for value in re.findall(r"WHERE shard=(\d+)", sql)]
                stamps = sorted(
                    stamp for shard in shards for stamp in outer._rows_by_shard.get(shard, [])
                )
                return [[stamps[0]]] if stamps else [[None]]

        return _Snapshot()


def _queried_shards(sql: str) -> set[int]:
    return {int(value) for value in re.findall(r"WHERE shard=(\d+)", sql)}


def test_spanner_oldest_enqueued_at_is_the_minimum_across_every_shard() -> None:
    """The oldest row can sit in any shard, so all 32 heads are read.

    A single global `ORDER BY commit_ts LIMIT 1` would be a table scan -- the
    primary key leads with `shard` -- and would get more expensive exactly as
    the backlog it measures grows.
    """
    oldest = dt.datetime(2026, 8, 2, 3, 0, tzinfo=dt.UTC)
    database = _SnapshotDatabase(
        {
            0: [dt.datetime(2026, 8, 17, 11, 0, tzinfo=dt.UTC)],
            7: [oldest, dt.datetime(2026, 8, 10, 0, 0, tzinfo=dt.UTC)],
            31: [dt.datetime(2026, 8, 5, 0, 0, tzinfo=dt.UTC)],
        }
    )
    outbox = SpannerOperationalAnalyticsOutbox(database, _ParamTypes())

    assert outbox.oldest_enqueued_at() == oldest
    [(sql, _)] = database.queries
    assert _queried_shards(sql) == set(range(OPERATIONAL_ANALYTICS_OUTBOX_SHARDS))
    assert sql.count("ORDER BY commit_ts LIMIT 1") == OPERATIONAL_ANALYTICS_OUTBOX_SHARDS
    assert "count(" not in sql.lower()


def test_spanner_lag_read_is_one_round_trip_not_one_per_shard() -> None:
    """32 sequential round trips do not fit the budget that bounds this read.

    Measured against production Spanner on 2026-08-17, the per-shard loop this
    replaced took 9.76s (2.22s for the first shard, ~0.25s for each of the
    rest) against a 3.0s budget, so it raised TimeoutError on EVERY call and
    /status.json published `unreachable` for a cloud whose outbox was empty --
    a healthy drain reported as a broken one, on the very page added to notice
    broken drains. As one statement: 2.93s cold, 1.01s warm.

    The count is the assertion. Anything that walks the shards in Python is
    correct and unusably slow, and it would pass every other test here.
    """
    database = _SnapshotDatabase({5: [dt.datetime(2026, 8, 2, 3, 0, tzinfo=dt.UTC)]})
    outbox = SpannerOperationalAnalyticsOutbox(database, _ParamTypes())

    outbox.oldest_enqueued_at(timeout=3.0)

    assert len(database.queries) == 1


def test_spanner_lag_read_never_scans_the_whole_table() -> None:
    """Every arm carries a shard predicate; a bare MIN() would scan.

    One round trip is achievable the wrong way -- `SELECT MIN(commit_ts) FROM
    tr_operational_analytics_outbox` is also one statement, and it degrades
    precisely as the backlog grows, which is when this number matters most.
    """
    database = _SnapshotDatabase({0: [dt.datetime(2026, 8, 2, 3, 0, tzinfo=dt.UTC)]})
    outbox = SpannerOperationalAnalyticsOutbox(database, _ParamTypes())

    outbox.oldest_enqueued_at()

    [(sql, _)] = database.queries
    selects = sql.count("FROM tr_operational_analytics_outbox")
    assert selects == OPERATIONAL_ANALYTICS_OUTBOX_SHARDS
    assert selects == sql.count("WHERE shard=")


def test_spanner_oldest_enqueued_at_is_none_when_every_shard_is_drained() -> None:
    """Fully drained is the healthiest state, and it is not an absence of data."""
    outbox = SpannerOperationalAnalyticsOutbox(_SnapshotDatabase({}), _ParamTypes())

    assert outbox.oldest_enqueued_at() is None


def test_spanner_oldest_enqueued_at_returns_utc_aware_timestamps() -> None:
    """A naive value would compare against `now` as if it were local time."""
    database = _SnapshotDatabase({3: [dt.datetime(2026, 8, 2, 3, 0)]})
    outbox = SpannerOperationalAnalyticsOutbox(database, _ParamTypes())

    result = outbox.oldest_enqueued_at()

    assert result is not None
    assert result.tzinfo is not None
    assert result == dt.datetime(2026, 8, 2, 3, 0, tzinfo=dt.UTC)


def test_spanner_oldest_enqueued_at_spends_one_budget_across_all_shards() -> None:
    """The bound is on the CALL, not on each of the 32 statements.

    This read runs on the public /status.json path inside an async handler, so
    the number that matters is how long the whole thing can hold the event
    loop. With one statement the two are the same thing by construction, which
    is the second reason to prefer it: the earlier form had to subtract
    elapsed time from a deadline to keep the promise this now keeps for free.
    """
    database = _SnapshotDatabase({0: [dt.datetime(2026, 8, 2, 3, 0, tzinfo=dt.UTC)]})
    outbox = SpannerOperationalAnalyticsOutbox(database, _ParamTypes())

    outbox.oldest_enqueued_at(timeout=5.0)

    assert database.timeouts == [5.0]


def test_activity_allowlist_carries_workspace_id_to_clickhouse() -> None:
    """The drain projects payloads onto ACTIVITY_COLUMNS; a key missing from
    the allowlist is dropped silently, which would ship this feature as a
    column of empty strings."""
    from clickhouse.ingest_operational_outbox import ACTIVITY_COLUMNS

    assert "workspace_id" in ACTIVITY_COLUMNS
    # Order stability: appended after tenant_id's group, never before
    # generation_id -- the archive row hash is computed over this tuple.
    assert ACTIVITY_COLUMNS.index("workspace_id") > ACTIVITY_COLUMNS.index("tenant_id")
