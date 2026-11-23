from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import hashlib
import inspect
import json
import random
import re
import sqlite3
import textwrap
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
from tests.fakes.clickhouse_activity import ActivityClickHouse
from trusted_router import operational_analytics as analytics_module
from trusted_router.client_events_schema import ClientEventsBatch
from trusted_router.operational_analytics import (
    OperationalAnalyticsClient,
    elapsed_histogram_columns,
    settle_elapsed_median,
    usage_buckets_query,
)
from trusted_router.storage_activity import (
    generation_events,
    generation_metrics,
    usage_bucket_key,
)
from trusted_router.storage_gcp import SpannerStore
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


def test_retirement_route_workspaces_exact_route_and_half_open_window() -> None:
    # Execute the shipped selection SQL on fixture rows, translating only
    # ClickHouse's parameter/timestamp/result syntax. Removing either route
    # predicate or a time boundary changes the resulting workspace ids.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE activity_generations (workspace_id TEXT, provider TEXT, model TEXT, created_at TEXT)")
    conn.executemany("INSERT INTO activity_generations VALUES (?, ?, ?, ?)", [
        ("start", "tinfoil", "model", "2026-08-02T12:00:00.000Z"),
        ("before", "tinfoil", "model", "2026-08-02T11:59:59.999Z"),
        ("recent", "tinfoil", "model", "2026-09-01T11:59:59.999Z"),
        ("recent", "tinfoil", "model", "2026-09-01T11:00:00.000Z"),
        ("end", "tinfoil", "model", "2026-09-01T12:00:00.000Z"),
        ("future", "tinfoil", "model", "2026-09-01T12:00:00.001Z"),
        ("other-provider", "near-ai", "model", "2026-09-01T11:00:00.000Z"),
        ("other-model", "tinfoil", "model-other", "2026-09-01T11:00:00.000Z"),
        ("", "tinfoil", "model", "2026-09-01T11:00:00.000Z"),
    ])

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.content.decode()
        assert "FROM activity_generations FINAL" in query
        assert "LIMIT" not in query
        query = query.replace(" FINAL", "").replace("FORMAT JSON", "")
        query = re.sub(r"parseDateTime64BestEffort\((\{\w+:String\}), 3\)", r"\1", query)
        query = re.sub(r"\{(\w+):String\}", r":\1", query)
        params = {key.removeprefix("param_"): value for key, value in request.url.params.items()}
        rows = conn.execute(query, params).fetchall()
        return httpx.Response(200, json={"data": [dict(row) for row in rows]})

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse.test", user="read", password="test",  # noqa: S106
        transport=httpx.MockTransport(handler),
    )
    try:
        assert client.route_workspaces(
            provider="tinfoil", model="model",
            start_at="2026-08-02T12:00:00.000Z", end_at="2026-09-01T12:00:00.000Z",
        ) == ["recent", "start"]
        assert client.route_workspaces(
            provider="tinfoil' OR 1=1 --", model="model",
            start_at="2026-08-02T12:00:00.000Z", end_at="2026-09-01T12:00:00.000Z",
        ) == []
    finally:
        conn.close()


def _generation() -> Generation:
    return Generation(
        id="gen-activity-1",
        request_id="req-activity-1",
        workspace_id="ws-private-123",
        key_hash="salted-key-hash-private",
        model="anthropic/claude-haiku-4.5",
        requested_model="nyte/auto-routing",
        provider="anthropic",
        provider_name="Anthropic",
        app="Test app",
        app_id="test-app",
        app_markup_microdollars=2,
        custom_model_markup_microdollars=3,
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
    assert payload["app_id"] == "test-app"
    assert payload["requested_model"] == "nyte/auto-routing"
    assert payload["app_markup_microdollars"] == 2
    assert payload["custom_model_markup_microdollars"] == 3
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


def test_gcp_route_health_batch_read_uses_clickhouse_directly() -> None:
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

    store = object.__new__(SpannerStore)
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
    # Rule: the window is the indexed column, and no JSON timestamp is parsed.
    assert "AND indexed_at >= %s::timestamptz" in sql
    assert "body ->> 'created_at'" not in sql
    assert params == ("2026-08-29T00:00:00Z", 48, 47_088)


def test_postgres_benchmark_write_indexes_created_at() -> None:
    statements: list[tuple[str, tuple[object, ...]]] = []

    class Connection:
        def execute(self, sql: str, params: tuple[object, ...]) -> None:
            statements.append((sql, params))

    store = PostgresStore.__new__(PostgresStore)
    store._run_transaction = lambda operation: operation(Connection())  # type: ignore[method-assign]
    store.record_provider_benchmark(
        ProviderBenchmarkSample(
            id="bench-postgres-write-1",
            model="openai/gpt-5.5",
            provider="openai",
            provider_name="OpenAI",
            status="success",
            usage_type="Credits",
            streamed=True,
            created_at="2026-09-25T01:02:03.456Z",
            source="synthetic",
        )
    )

    [(sql, params)] = statements
    assert "INSERT INTO tr_entities" in sql and "indexed_at" in sql
    assert params[:2] == ("provider_benchmark", "bench-postgres-write-1")
    assert params[3] == dt.datetime(2026, 9, 25, 1, 2, 3, 456000, tzinfo=dt.UTC)


def test_postgres_benchmark_backfill_page_parses_like_the_writer() -> None:
    statements: list[tuple[str, tuple[object, ...]]] = []
    page = [
        ("bench-a", "2026-09-25T01:00:00Z"),
        ("bench-b", "not-a-timestamp"),
        ("bench-c", "2026-09-25T02:00:00"),
        ("bench-d", "0001-01-01T00:00:00+01:00"),
    ]

    class Cursor:
        def fetchall(self) -> list[tuple[str, str]]:
            return page

    class Connection:
        def execute(self, sql: str, params: tuple[object, ...]) -> Cursor:
            statements.append((sql, params))
            return Cursor()

    store = PostgresStore.__new__(PostgresStore)
    store._run_transaction = lambda operation: operation(Connection())  # type: ignore[method-assign]

    examined = store.backfill_provider_benchmark_indexed_at_page(after="bench-0", limit=5_000)

    assert examined == ["bench-a", "bench-b", "bench-c", "bench-d"]
    (select_sql, select_params), *updates = statements
    assert "indexed_at IS NULL" in select_sql and "FOR UPDATE" in select_sql
    assert select_params == ("bench-0", 1_000)
    assert [params for _, params in updates] == [
        (dt.datetime(2026, 9, 25, 1, 0, tzinfo=dt.UTC), "bench-a"),
        (dt.datetime(2026, 9, 25, 2, 0, tzinfo=dt.UTC), "bench-c"),
    ]
    assert all("indexed_at IS NULL" in sql for sql, _ in updates)


@pytest.mark.parametrize(
    "snapshot_name",
    [
        "leaderboard",
        "leaderboard_evidence",
        "apps",
        "video_leaderboard",
        "status_inputs",
        "client_reliability",
        "model_routing_evidence",
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
    observed: dict[str, float | bool] = {}

    def query(_sql, *, params=None, timeout_seconds=20.0, retry_read_disconnect=False):
        _ = params
        observed["timeout_seconds"] = timeout_seconds
        observed["retry_read_disconnect"] = retry_read_disconnect
        return []

    monkeypatch.setattr(client, "_query", query)

    assert client.public_snapshot("leaderboard") is None
    assert observed == {"timeout_seconds": 2.0, "retry_read_disconnect": True}


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
    assert event.row["app_id"] == "test-app"
    assert event.row["requested_model"] == "nyte/auto-routing"
    assert event.row["app_markup_microdollars"] == 2
    assert event.row["custom_model_markup_microdollars"] == 3
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


@pytest.mark.parametrize(
    "app_fields",
    [
        {},
        {"app_id": None, "app_markup_microdollars": None, "custom_model_markup_microdollars": None},
        {"app_id": None, "app_markup_microdollars": 0, "custom_model_markup_microdollars": 0},
        {"app_id": "test-app", "app_markup_microdollars": "2", "custom_model_markup_microdollars": "3"},
    ],
    ids=["missing", "null", "defaults", "attributed"],
)
def test_clickhouse_activity_reader_binds_private_filters_and_never_sends_raw_ids(
    app_fields: dict[str, Any],
) -> None:
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
        selected = body.split("FROM", 1)[0]
        assert "requested_model" in selected
        assert "app_id" in selected
        assert "app_markup_microdollars" in selected
        assert "custom_model_markup_microdollars" in selected
        assert "operator_cost_microdollars" not in selected
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
                        **app_fields,
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
    public = generation.to_openrouter_generation()
    [event] = generation_events([generation])
    assert public["app_id"] == event.get("app_id") == (app_fields.get("app_id") or None)
    for field in ("app_markup_microdollars", "custom_model_markup_microdollars"):
        assert public[field] == event[field] == int(app_fields.get(field) or 0)
    assert "operator_cost_microdollars" not in public
    assert "operator_cost_microdollars" not in event


@pytest.mark.parametrize(
    ("granularity", "expression"),
    [
        ("minute", "formatDateTime(created_at, '%Y-%m-%dT%H:%i', 'UTC') AS bucket"),
        (
            "5min",
            "formatDateTime(toStartOfFiveMinutes(created_at), '%Y-%m-%dT%H:%i', 'UTC') AS bucket",
        ),
        ("hour", "formatDateTime(created_at, '%Y-%m-%dT%H', 'UTC') AS bucket"),
        ("day", "formatDateTime(created_at, '%Y-%m-%d', 'UTC') AS bucket"),
    ],
)
def test_usage_buckets_query_labels_each_granularity_in_utc(
    granularity: str, expression: str
) -> None:
    sql = usage_buckets_query(granularity=granularity, by_model=False, key_filtered=False)

    assert expression in sql
    # %M is the month name in ClickHouse. A minute label must never use it.
    assert "%M" not in sql
    assert "FROM activity_generations FINAL" in sql
    assert "GROUP BY bucket, usage_type\n" in sql
    assert "ORDER BY bucket, usage_type\n" in sql
    # Grouped, so the result is small and nothing is cut off.
    assert "LIMIT" not in sql
    assert sql.rstrip().endswith("FORMAT JSON")


def test_usage_buckets_query_rejects_an_unknown_granularity() -> None:
    with pytest.raises(ValueError, match="unknown granularity: week"):
        usage_buckets_query(granularity="week", by_model=False, key_filtered=False)


def test_usage_buckets_query_groups_by_model_only_when_asked() -> None:
    plain = usage_buckets_query(granularity="day", by_model=False, key_filtered=False)
    by_model = usage_buckets_query(granularity="day", by_model=True, key_filtered=False)

    assert "model" not in plain.split("FROM", 1)[0]
    assert "\n  model,\n" in by_model.split("FROM", 1)[0]
    assert "GROUP BY bucket, model, usage_type\n" in by_model
    assert "ORDER BY bucket, model, usage_type\n" in by_model


def test_usage_buckets_query_filters_are_bound_parameters() -> None:
    unfiltered = usage_buckets_query(granularity="hour", by_model=True, key_filtered=False)
    filtered = usage_buckets_query(granularity="hour", by_model=True, key_filtered=True)

    assert "key_id" not in unfiltered
    assert "AND key_id = {key_id:String}" in filtered
    for sql in (unfiltered, filtered):
        assert "WHERE tenant_id = {tenant_id:String}" in sql
        assert "AND created_at >= parseDateTime64BestEffort({start_at:String}, 3)" in sql
        assert "AND created_at < parseDateTime64BestEffort({end_at:String}, 3)" in sql
    # The text depends on three fixed choices only, never on a request value.
    assert set(re.findall(r"\{(\w+):String\}", filtered)) == {
        "tenant_id",
        "key_id",
        "start_at",
        "end_at",
    }


def test_usage_buckets_query_groups_the_stored_usage_type_without_alias_cycles() -> None:
    sql = usage_buckets_query(granularity="day", by_model=False, key_filtered=False)

    # The stored value is a grouping key. Credits or BYOK is decided by the
    # caller with UsageType.coerce, never by a string comparison in SQL, which
    # would trim and fold case differently from Python.
    assert "\n  usage_type,\n" in sql.split("FROM", 1)[0]
    assert "sum(total_cost_microdollars) AS cost_total" in sql
    assert "byok" not in sql.lower()
    # ClickHouse resolves a name inside an aggregate to a same-named alias,
    # so an alias must not repeat the column it sums.
    aliases = re.findall(r"\bAS (\w+)", sql)
    columns = {
        "tokens_prompt",
        "tokens_completion",
        "reasoning_tokens",
        "total_cost_microdollars",
        "usage_type",
        "created_at",
        "model",
    }
    assert aliases == [
        "bucket",
        "requests",
        "prompt_tokens",
        "completion_tokens",
        "reasoning_token_total",
        "cost_total",
    ]
    assert columns.isdisjoint(aliases)


# Stored values as the column could hold them, canonical or not, with the
# whitespace and case forms Python's strip() and lower() accept.
_STORED_USAGE_TYPES = [
    "BYOK",
    "Credits",
    "byok",
    " Byok ",
    "\tBYOK\t",
    "byok\n",
    "\u2003BYOK\u00a0",  # Unicode spaces, which strip() removes too
    "BYO\u212a",  # KELVIN SIGN lower-cases to "k"
    "credits",
    "",
    "byok-ish",
    "b yok",
]


@pytest.mark.parametrize("stored", _STORED_USAGE_TYPES)
def test_clickhouse_usage_reader_decides_byok_exactly_as_generation_metrics(stored: str) -> None:
    """Whatever the column holds, the split is the one the row-by-row path made."""
    # The row-by-row path: the reader coerced the stored value into the
    # generation, and generation_metrics split the cost on it.
    generation = dataclasses.replace(
        _generation(), usage_type=UsageType.coerce(stored), total_cost_microdollars=7
    )
    expected = generation_metrics(generation)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "bucket": "2026-07-31",
                        "usage_type": stored,
                        "requests": 1,
                        "prompt_tokens": generation.tokens_prompt,
                        "completion_tokens": generation.tokens_completion,
                        "reasoning_token_total": generation.reasoning_tokens,
                        "cost_total": 7,
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
    [row] = client.activity_usage_buckets(
        tenant_id="tenant", granularity="day", start_at="2026-07-31", end_at="2026-08-01"
    )

    assert {metric: row[metric] for metric in expected} == expected
    assert {row["cost_micro"], row["byok_micro"]} == {0, 7}


@pytest.mark.parametrize("key_filtered", [False, True])
def test_clickhouse_usage_reader_binds_filters_and_never_sends_raw_ids(
    key_filtered: bool,
) -> None:
    tenant_id = analytics_surrogate("workspace", "ws-private-123")
    key_id = analytics_surrogate("api-key", "key-hash")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # 64-bit integers arrive quoted or bare, depending on a server setting.
        return httpx.Response(
            200,
            json={
                "data": [
                    # One bucket and model, two stored usage types: one row out.
                    {
                        "bucket": "2026-07-31T12",
                        "model": "anthropic/claude-haiku-4.5",
                        "usage_type": "BYOK",
                        "requests": 1,
                        "prompt_tokens": "12",
                        "completion_tokens": 3,
                        "reasoning_token_total": "1",
                        "cost_total": 9,
                    },
                    {
                        "bucket": "2026-07-31T12",
                        "model": "anthropic/claude-haiku-4.5",
                        "usage_type": "Credits",
                        "requests": "2",
                        "prompt_tokens": "24",
                        "completion_tokens": 6,
                        "reasoning_token_total": "2",
                        "cost_total": "18",
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
    rows = client.activity_usage_buckets(
        tenant_id=tenant_id,
        key_id=key_id if key_filtered else None,
        granularity="hour",
        start_at="2026-07-31T00:00:00",
        end_at="2026-08-01",
        by_model=True,
    )

    assert rows == [
        {
            "bucket": "2026-07-31T12",
            "model": "anthropic/claude-haiku-4.5",
            "requests": 3,
            "prompt_tokens": 36,
            "completion_tokens": 9,
            "reasoning_tokens": 3,
            "cost_micro": 18,
            "byok_micro": 9,
        }
    ]
    [request] = requests
    params = {
        key: value for key, value in request.url.params.items() if key.startswith("param_")
    }
    expected = {
        "param_tenant_id": tenant_id,
        "param_start_at": "2026-07-31T00:00:00",
        "param_end_at": "2026-08-01",
    }
    if key_filtered:
        expected["param_key_id"] = key_id
    assert params == expected
    body = request.content.decode()
    assert body == usage_buckets_query(granularity="hour", by_model=True, key_filtered=key_filtered)
    for value in (tenant_id, key_id, "2026-07-31", "ws-private-123", "key-hash"):
        assert value not in body


def test_clickhouse_usage_reader_runs_the_shipped_sql_on_fixture_rows() -> None:
    """Bucket labels, the usage-type split and FINAL, executed rather than read."""
    database = ActivityClickHouse()
    credits = dataclasses.replace(
        _generation(), id="gen-credits", created_at="2026-07-31T23:59:59.999Z"
    )
    byok = dataclasses.replace(
        _generation(),
        id="gen-byok",
        usage_type=UsageType.BYOK,
        total_cost_microdollars=40,
        created_at="2026-08-01T00:00:00.000Z",
    )
    other_tenant = dataclasses.replace(_generation(), id="gen-other", workspace_id="ws-other")
    for generation in (credits, byok, other_tenant):
        database.add(generation)
    # A replayed delivery of the same generation counts once under FINAL.
    database.add(credits)
    # The same day's BYOK spend under a spelling SQL trimming would miss.
    database.add(
        dataclasses.replace(byok, id="gen-byok-tabbed", total_cost_microdollars=2),
        usage_type="\tBYOK\t",
    )
    client = database.client()
    tenant_id = analytics_surrogate("workspace", credits.workspace_id)

    def buckets(granularity: str) -> list[dict[str, Any]]:
        return client.activity_usage_buckets(
            tenant_id=tenant_id,
            granularity=granularity,
            start_at="2026-07-31",
            end_at="2026-08-02",
        )

    assert buckets("day") == [
        {
            "bucket": "2026-07-31",
            "requests": 1,
            "prompt_tokens": 12,
            "completion_tokens": 3,
            "reasoning_tokens": 1,
            "cost_micro": 9,
            "byok_micro": 0,
        },
        {
            "bucket": "2026-08-01",
            "requests": 2,
            "prompt_tokens": 24,
            "completion_tokens": 6,
            "reasoning_tokens": 2,
            "cost_micro": 0,
            "byok_micro": 42,
        },
    ]
    for granularity in ("minute", "5min", "hour", "day"):
        rows = buckets(granularity)
        assert [row["bucket"] for row in rows] == [
            usage_bucket_key(credits.created_at, granularity),
            usage_bucket_key(byok.created_at, granularity),
        ]
    # The half-open window: the end bound excludes the row that sits on it.
    assert [
        row["bucket"]
        for row in client.activity_usage_buckets(
            granularity="day",
            tenant_id=tenant_id,
            start_at="2026-07-31T23:59:59.999Z",
            end_at="2026-08-01T00:00:00.000Z",
        )
    ] == ["2026-07-31"]
    assert all("FROM activity_generations FINAL" in query for query in database.queries)


def test_activity_elapsed_summary_is_a_whole_range_median_in_sql() -> None:
    database = ActivityClickHouse()
    base = _generation()
    tenant_id = analytics_surrogate("workspace", base.workspace_id)
    for index, elapsed in enumerate([40, None, 10, 31, 20]):
        database.add(
            dataclasses.replace(
                base,
                id=f"gen-elapsed-{index}",
                elapsed_milliseconds=elapsed,
                created_at=f"2026-07-31T12:00:0{index}.000Z",
            )
        )
    database.add(dataclasses.replace(base, id="gen-before", created_at="2026-07-31T11:59:59.999Z"))
    database.add(dataclasses.replace(base, id="gen-other-tenant", workspace_id="ws-other"))
    client = database.client()

    # Four timed rows: the floor of the mean of the two middle values.
    assert client.activity_elapsed_summary(
        tenant_id=tenant_id, start_at="2026-07-31T12:00:00Z"
    ) == {"generations": 5, "samples": 4, "median_ms": 25}
    # An odd count is the middle value.
    assert client.activity_elapsed_summary(
        tenant_id=tenant_id, start_at="2026-07-31T12:00:02Z"
    ) == {"generations": 3, "samples": 3, "median_ms": 20}
    # No timed rows: no median, not zero.
    assert client.activity_elapsed_summary(
        tenant_id=tenant_id, start_at="2026-08-01T00:00:00Z"
    ) == {"generations": 0, "samples": 0, "median_ms": None}
    [query] = set(database.queries)
    assert "FROM activity_generations FINAL" in query
    # Rows are counted per slot and, inside the middle slots, per value. An
    # exact quantile function would keep every row's value in memory.
    assert "GROUP BY slot, elapsed_value" in query
    assert "quantile" not in query
    assert "LIMIT" not in query
    assert tenant_id not in query
    # The SQL's literals are the module's histogram constants.
    assert f"WHEN elapsed_milliseconds >= {analytics_module.ELAPSED_CEILING_MS} " in query
    assert f"THEN {analytics_module.ELAPSED_LAST_SLOT}\n" in query
    assert f"WHEN elapsed_milliseconds < {analytics_module.ELAPSED_CEILING_MS}\n" in query
    assert query.count(f"intDiv(elapsed_milliseconds, {analytics_module.ELAPSED_SLOT_MS})") == 2
    # The first read opens no slot; the second opens the slot of the middle values.
    assert database.params[:2] == [
        {
            "tenant_id": tenant_id,
            "start_at": "2026-07-31T12:00:00Z",
            "slot_low": "65535",
            "slot_high": "65535",
        },
        {
            "tenant_id": tenant_id,
            "start_at": "2026-07-31T12:00:00Z",
            "slot_low": "0",
            "slot_high": "0",
        },
    ]
    # Nothing timed in the range: one read is enough.
    assert len(database.queries) == 5


def _row_by_row_median(values: list[int]) -> int | None:
    """The median the page computed itself, before the SQL did."""
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) // 2


def test_activity_elapsed_summary_equals_the_row_by_row_median() -> None:
    """Odd and even counts, repeated values, untimed rows, one row, none."""
    database = ActivityClickHouse()
    base = _generation()
    randomness = random.Random(20261121)  # noqa: S311 - fixture data, not a secret
    cases: list[list[int | None]] = [
        [],
        [None],
        [7],
        [7, None],
        [1, 2],
        [2, 1, 2, 2],
        [5, 5, 5, 9],
        [3, 4, 4, 4, 9, 9],
        # The two middle values in different slots, adjacent and far apart.
        [1_023, 1_024],
        [10, 2_000_000],
        [1_023, 1_023, 1_024, 5_000],
        # The last value before the open-ended slot, and an outlier inside it.
        [67_107_839, 67_107_839],
        [5, 6, 1_000_000_000_000],
    ]
    for _ in range(40):
        size = randomness.randint(1, 60)
        spread = randomness.choice([2, 5, 1_000, 3_000, 200_000])
        cases.append(
            [
                None if randomness.random() < 0.2 else randomness.randrange(spread)
                for _ in range(size)
            ]
        )
    for number, values in enumerate(cases):
        workspace = f"ws-median-{number}"
        for index, elapsed in enumerate(values):
            database.add(
                dataclasses.replace(
                    base,
                    id=f"gen-{number}-{index}",
                    workspace_id=workspace,
                    elapsed_milliseconds=elapsed,
                    created_at=f"2026-07-31T12:{index:02d}:00.000Z",
                )
            )
    client = database.client()

    for number, values in enumerate(cases):
        timed = [value for value in values if value is not None]
        assert client.activity_elapsed_summary(
            tenant_id=analytics_surrogate("workspace", f"ws-median-{number}"),
            start_at="2026-07-31T00:00:00Z",
        ) == {
            "generations": len(values),
            "samples": len(timed),
            "median_ms": _row_by_row_median(timed),
        }, values


@pytest.mark.parametrize(
    "values",
    [
        [67_107_840],  # the first value of the open-ended slot
        [1_000_000_000_000, 1_000_000_000_001],
        [5, 67_107_840],  # an even count whose upper middle is out of range
        [5, 67_107_840, 67_107_841],
    ],
)
def test_activity_elapsed_summary_reports_no_median_beyond_the_histogram(
    values: list[int],
) -> None:
    """A median of about 18.6 hours or more is absent, never approximated."""
    database = ActivityClickHouse()
    base = _generation()
    for index, elapsed in enumerate(values):
        database.add(
            dataclasses.replace(base, id=f"gen-far-{index}", elapsed_milliseconds=elapsed)
        )

    assert database.client().activity_elapsed_summary(
        tenant_id=analytics_surrogate("workspace", base.workspace_id),
        start_at="2026-07-31T00:00:00Z",
    ) == {"generations": len(values), "samples": len(values), "median_ms": None}
    # The open-ended slot is never opened: one read, one row per slot.
    assert len(database.queries) == 1
    assert database.returned == [len({min(value // 1024, 65_535) for value in values})]


def test_activity_elapsed_summary_reads_a_bounded_histogram_not_the_rows() -> None:
    """9,000 rows over 9,000 distinct values: what comes back is slots, then one slot's values."""
    database = ActivityClickHouse()
    base = _generation()
    values = [value * 7 for value in range(9_000)]  # 0 .. 62,993 ms: 62 slots
    for index, elapsed in enumerate(values):
        database.add(
            dataclasses.replace(base, id=f"gen-wide-{index}", elapsed_milliseconds=elapsed)
        )

    summary = database.client().activity_elapsed_summary(
        tenant_id=analytics_surrogate("workspace", base.workspace_id),
        start_at="2026-07-31T00:00:00Z",
    )

    assert summary == {
        "generations": 9_000,
        "samples": 9_000,
        "median_ms": _row_by_row_median(values),
    }
    slots = len({value // 1024 for value in values})
    in_middle_slot = len([value for value in values if value // 1024 == 31_496 // 1024])
    assert slots == 62 and in_middle_slot < 1_024
    # First the slots alone, then the slots with the middle one opened.
    assert database.returned == [slots, slots - 1 + in_middle_slot]


def _histogram_client(
    answers: list[list[dict[str, Any]]], asked: list[tuple[str, str]]
) -> OperationalAnalyticsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(
            (request.url.params["param_slot_low"], request.url.params["param_slot_high"])
        )
        return httpx.Response(200, json={"data": answers[len(asked) - 1]})

    return OperationalAnalyticsClient(
        base_url="http://clickhouse.test:8123",
        user="reader",
        password="secret",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(handler),
    )


def _slot(slot: int | None, count: int, value: int | None = None) -> dict[str, Any]:
    # 64-bit integers arrive quoted or bare, depending on a server setting.
    return {
        "slot": None if slot is None else str(slot),
        "elapsed_value": value,
        "generations_at_value": str(count),
    }


def test_activity_elapsed_summary_follows_a_middle_that_moved_between_reads() -> None:
    """Rows that arrive between two reads can move the middle to another slot.

    Each read carries its own totals, so a read that opened the wrong slot
    shows it, and the next read opens the right one. The answer is never
    assembled from two different snapshots.
    """
    asked: list[tuple[str, str]] = []
    client = _histogram_client(
        [
            # 4 timed rows, all in slot 2: both middle values are there.
            [_slot(2, 4), _slot(None, 1)],
            # By the second read 7 slower rows have arrived: 11 timed rows,
            # and the middle (rank 6) is now in slot 9, which is not open.
            [
                _slot(2, 1, 2_050),
                _slot(2, 3, 2_060),
                _slot(9, 7),
                _slot(None, 1),
            ],
            # The third read opens slot 9 and settles on its own counts.
            [_slot(2, 4), _slot(9, 3, 9_300), _slot(9, 4, 9_400), _slot(None, 1)],
        ],
        asked,
    )

    assert client.activity_elapsed_summary(tenant_id="tenant", start_at="2026-07-31") == {
        "generations": 12,
        "samples": 11,
        "median_ms": 9_300,
    }
    assert asked == [("65535", "65535"), ("2", "2"), ("9", "9")]


def test_activity_elapsed_summary_fails_rather_than_guess_when_reads_never_agree() -> None:
    asked: list[tuple[str, str]] = []
    # Every read finds the middle in a slot the read before it did not open.
    answers = [[_slot(read + 1, 3)] for read in range(10)]
    client = _histogram_client(answers, asked)

    with pytest.raises(RuntimeError, match="did not settle"):
        client.activity_elapsed_summary(tenant_id="tenant", start_at="2026-07-31")
    assert asked == [("65535", "65535"), ("1", "1"), ("2", "2"), ("3", "3")]


def test_activity_elapsed_summary_opens_two_slots_for_an_even_count() -> None:
    asked: list[tuple[str, str]] = []
    client = _histogram_client(
        [
            [_slot(0, 3), _slot(7, 3)],
            [
                _slot(0, 1, 10),
                _slot(0, 2, 1_000),
                _slot(7, 1, 7_200),
                _slot(7, 2, 8_000),
            ],
        ],
        asked,
    )

    # Six timed rows: the third (1,000) and the fourth (7,200).
    assert client.activity_elapsed_summary(tenant_id="tenant", start_at="2026-07-31") == {
        "generations": 6,
        "samples": 6,
        "median_ms": 4_100,
    }
    assert asked == [("65535", "65535"), ("0", "7")]


def test_activity_elapsed_summary_does_not_depend_on_the_order_rows_come_back_in() -> None:
    """ClickHouse returns grouped rows in no particular order."""
    coarse = [_slot(0, 2), _slot(3, 4), _slot(5, 1), _slot(None, 2), _slot(8, 3)]
    opened = [
        _slot(0, 2),
        _slot(3, 1, 3_500),
        _slot(3, 1, 3_100),
        _slot(3, 2, 3_300),
        _slot(5, 1, 5_200),
        _slot(None, 2),
        _slot(8, 3),
    ]
    # Ten timed rows: 0, 0, 3100, 3300, 3300, 3500, 5200, then three in slot 8.
    expected = {"generations": 12, "samples": 10, "median_ms": (3_300 + 3_500) // 2}
    randomness = random.Random(7)  # noqa: S311 - fixture order, not a secret
    for _ in range(20):
        asked: list[tuple[str, str]] = []
        first, second = list(coarse), list(opened)
        randomness.shuffle(first)
        randomness.shuffle(second)
        client = _histogram_client([first, second], asked)

        assert client.activity_elapsed_summary(tenant_id="tenant", start_at="2026-07-31") == expected
        assert asked == [("65535", "65535"), ("3", "3")]


def test_elapsed_histogram_columns_take_the_latency_expression_and_the_open_condition() -> None:
    columns = elapsed_histogram_columns(
        "first_token_milliseconds", "intDiv(first_token_milliseconds, 1024) = {slot:UInt64}"
    )

    assert columns == (
        "  CASE\n"
        "    WHEN first_token_milliseconds IS NULL THEN NULL\n"
        "    WHEN first_token_milliseconds >= 67107840 THEN 65535\n"
        "    ELSE intDiv(first_token_milliseconds, 1024)\n"
        "  END AS slot,\n"
        "  CASE\n"
        "    WHEN first_token_milliseconds < 67107840\n"
        "      AND intDiv(first_token_milliseconds, 1024) = {slot:UInt64}\n"
        "    THEN first_token_milliseconds\n"
        "  END AS elapsed_value"
    )
    # The numbers in the SQL are the module's histogram constants.
    assert analytics_module.ELAPSED_SLOT_MS == 1024
    assert analytics_module.ELAPSED_LAST_SLOT == 65_535
    assert analytics_module.ELAPSED_CEILING_MS == 67_107_840


def test_elapsed_histogram_serves_a_median_per_group() -> None:
    """The same two pieces give an exact median for each value of another key.

    The grouped query adds its key in front of ``slot, elapsed_value``; each
    group's rows settle on their own, and each group has its own slots to open.
    """
    database = ActivityClickHouse()
    base = _generation()
    elapsed_by_model: dict[str, list[int | None]] = {
        "model-fast": [100, 120, 130, 150, None],
        "model-slow": [9_000, 9_500, 40_000, 41_000, 41_500, 90_000],
        "model-untimed": [None, None],
    }
    for model, values in elapsed_by_model.items():
        for index, elapsed in enumerate(values):
            database.add(
                dataclasses.replace(
                    base, id=f"gen-{model}-{index}", model=model, elapsed_milliseconds=elapsed
                )
            )
    client = database.client()
    sql = (
        "\nSELECT\n  model,\n"
        + elapsed_histogram_columns(
            "elapsed_milliseconds",
            "model = {model:String}"
            " AND intDiv(elapsed_milliseconds, 1024) IN ({slot_low:UInt64}, {slot_high:UInt64})",
        )
        + ",\n  count() AS generations_at_value\n"
        "FROM activity_generations FINAL\n"
        "WHERE tenant_id = {tenant_id:String}\n"
        "GROUP BY model, slot, elapsed_value\n"
        "FORMAT JSON\n"
    )
    tenant_id = analytics_surrogate("workspace", base.workspace_id)

    def read(model: str, slots: tuple[int, int]) -> dict[str, list[dict[str, Any]]]:
        rows = client._query(
            sql,
            params={
                "tenant_id": tenant_id,
                "model": model,
                "slot_low": slots[0],
                "slot_high": slots[1],
            },
        )
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            groups.setdefault(str(row["model"]), []).append(row)
        return groups

    last = analytics_module.ELAPSED_LAST_SLOT
    medians: dict[str, int | None] = {}
    for model, rows in read("", (last, last)).items():
        settled = settle_elapsed_median(rows)
        if not isinstance(settled, dict):
            settled = settle_elapsed_median(read(model, settled)[model])
        assert isinstance(settled, dict)
        medians[model] = settled["median_ms"]

    assert medians == {
        model: _row_by_row_median([value for value in values if value is not None])
        for model, values in elapsed_by_model.items()
    }
    assert medians == {"model-fast": 125, "model-slow": 40_500, "model-untimed": None}


def test_activity_elapsed_summary_counts_a_replayed_generation_once() -> None:
    database = ActivityClickHouse()
    base = _generation()
    for index, elapsed in enumerate([10, 20, 900]):
        database.add(
            dataclasses.replace(base, id=f"gen-replay-{index}", elapsed_milliseconds=elapsed)
        )
    # Replays of the slow row would drag the median to 900 if they counted.
    for _ in range(3):
        database.add(dataclasses.replace(base, id="gen-replay-2", elapsed_milliseconds=900))

    assert database.client().activity_elapsed_summary(
        tenant_id=analytics_surrogate("workspace", base.workspace_id),
        start_at="2026-07-31T00:00:00Z",
    ) == {"generations": 3, "samples": 3, "median_ms": 20}


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
        self.query_types: list[dict[str, Any]] = []

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
                params = kwargs.get("params") or {}
                outer.query_types.append(kwargs.get("param_types") or {})
                arms = re.findall(r"WHERE shard=(\d+)(?: AND commit_ts (>=|>) @(floor_\d+))?", sql)
                stamps = sorted(
                    stamp
                    for shard, operator, name in arms
                    for stamp in outer._rows_by_shard.get(int(shard), [])
                    if not name
                    or (stamp >= params[name] if operator == ">=" else stamp > params[name])
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


@pytest.mark.parametrize("floors", [None, {}])
def test_spanner_lag_no_floor_sql_golden(floors: dict[int, dt.datetime] | None) -> None:
    database = _SnapshotDatabase({})
    outbox = SpannerOperationalAnalyticsOutbox(database, _ParamTypes())
    outbox.oldest_enqueued_at(floors=floors)
    [(sql, params)] = database.queries
    # SHA256 of the complete pre-change 32-arm statement, including whitespace.
    assert hashlib.sha256(sql.encode()).hexdigest() == (
        "07b0982cd926e7a767e3b29d742a4bfd8aa2605da6c4179c46240186bf1a0cdb"
    )
    assert params == {}
    assert database.query_types == [{}]


def test_spanner_lag_floor_is_inclusive_and_specific_to_each_shard() -> None:
    floor = dt.datetime(2026, 9, 5, tzinfo=dt.UTC)
    later = floor + dt.timedelta(seconds=10)
    earlier = floor - dt.timedelta(seconds=10)
    database = _SnapshotDatabase({0: [earlier, floor, later], 1: [later]})
    outbox = SpannerOperationalAnalyticsOutbox(database, _ParamTypes(), shard_count=3)
    floors = {0: floor, 1: later}
    assert outbox.oldest_enqueued_at(floors=floors, timeout=3.0) == floor
    [(sql, params)] = database.queries
    assert params == {"floor_0": floor, "floor_1": later}
    assert database.query_types == [{"floor_0": "TIMESTAMP", "floor_1": "TIMESTAMP"}]
    assert "WHERE shard=2 ORDER BY commit_ts LIMIT 1" in sql
    assert database.timeouts == [3.0]
    # An unknown shard's backlog must not inherit another shard's floor.
    database._rows_by_shard[2] = [earlier]
    assert outbox.oldest_enqueued_at(floors=floors) == earlier
    assert floors == {0: floor, 1: later}


class _LiveOutboxDatabase:
    """Strong reads and staged deletes: rows disappear only on batch commit."""

    def __init__(self, rows: list[OperationalOutboxRow]) -> None:
        self.rows = list(rows)
        self.failure = ""
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.before_commit: Any = lambda: None

    def snapshot(self, **_kwargs: Any) -> Any:
        database = self

        class Snapshot:
            def __enter__(self) -> Any:
                return self

            def __exit__(self, *_args: Any) -> None:
                pass

            def execute_sql(self, sql: str, *, params: Any, param_types: Any) -> Any:
                database.queries.append((sql, dict(params)))
                if database.failure == "read":
                    raise RuntimeError("read failed")
                floor_name = "after" if "after" in params else "floor"
                floor = params.get(floor_name, _WATERMARK_EPOCH)
                inclusive = f"commit_ts >= @{floor_name}" in sql
                shards = params.get("shards", [params.get("shard")])
                rows = sorted(
                    (row for row in database.rows if row.shard in shards
                     and (row.commit_ts >= floor if inclusive else row.commit_ts > floor)),
                    key=lambda row: row.commit_ts,
                )
                if sql.startswith("SELECT commit_ts"):
                    return [(row.commit_ts,) for row in rows[:1]]
                return [(row.shard, row.commit_ts, row.event_kind, row.event_id, row.payload)
                        for row in rows[:params["limit"]]]

        return Snapshot()

    def batch(self) -> Any:
        database = self

        class Batch:
            def __init__(self) -> None:
                self.keys: list[Any] = []

            def __enter__(self) -> Any:
                return self

            def delete(self, _table: str, key_set: Any) -> None:
                self.keys = key_set.keys

            def __exit__(self, *_args: Any) -> None:
                database.before_commit()
                if database.failure == "commit":
                    raise RuntimeError("delete commit failed")
                database.rows = [row for row in database.rows if list(row.key) not in self.keys]

        return Batch()


def _live_spanner_source(
    monkeypatch: pytest.MonkeyPatch, database: _LiveOutboxDatabase,
) -> SpannerOperationalOutboxSource:
    from types import SimpleNamespace

    from google.cloud import spanner

    monkeypatch.setattr(spanner, "Client", lambda **_kwargs: SimpleNamespace(
        instance=lambda _name: SimpleNamespace(database=lambda _name: database),
    ))
    return SpannerOperationalOutboxSource(project="fake", instance="fake", database="fake")


def test_spanner_floor_keeps_ties_across_shards_and_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _outbox_row()
    tied = dataclasses.replace(first, shard=31, event_id="tie")
    later = dataclasses.replace(first, commit_ts=first.commit_ts + dt.timedelta(seconds=1))
    database = _LiveOutboxDatabase([first, tied, later])
    source = _live_spanner_source(monkeypatch, database)

    def before_commit() -> None:
        assert source._after is None, "floor advanced before delete commit"
        assert first in database.rows

    database.before_commit = before_commit
    drain_once(source, _Writer(), batch_size=1)
    assert source._after == first.commit_ts
    assert source.fetch(limit=1) == [tied]
    assert source.oldest_commit_ts() == tied.commit_ts
    # Construct through __init__, not a test-only reset of the attribute.
    restarted = _live_spanner_source(monkeypatch, database)
    assert restarted._after is None
    assert restarted.fetch(limit=1) == [tied]
    assert database.queries[-1][1]["after"] == _WATERMARK_EPOCH


@pytest.mark.parametrize("failure", ["fetch", "insert", "delete"])
def test_spanner_warm_floor_clears_on_every_drain_error(
    monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    first = _outbox_row()
    next_row = dataclasses.replace(first, event_id="next")
    database = _LiveOutboxDatabase([first, next_row])
    source = _live_spanner_source(monkeypatch, database)
    drain_once(source, _Writer(), batch_size=1)
    assert source._after == first.commit_ts
    with pytest.raises(RuntimeError):
        if failure == "insert":
            drain_once(source, _Writer(failures=1), batch_size=1)
        elif failure == "delete":
            database.failure = "commit"
            source.delete(source.fetch(limit=1))
        else:
            database.failure = "read"
            source.fetch(limit=1)
    assert source._after is None
    assert database.rows == [next_row]
    database.failure = ""
    assert source.fetch(limit=1) == [next_row]
    assert database.queries[-1][1]["after"] == _WATERMARK_EPOCH


def test_spanner_head_read_failure_keeps_the_warm_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``oldest_commit_ts`` is advisory (the heartbeat publisher calls it after
    every pass). Its failure says nothing about committed deletes, so the floor
    stays and the next fetch seeks from it, not from the epoch."""
    first = _outbox_row()
    next_row = dataclasses.replace(first, event_id="next")
    database = _LiveOutboxDatabase([first, next_row])
    source = _live_spanner_source(monkeypatch, database)
    drain_once(source, _Writer(), batch_size=1)
    assert source._after == first.commit_ts

    database.failure = "read"
    with pytest.raises(RuntimeError):
        source.oldest_commit_ts()
    assert source._after == first.commit_ts

    database.failure = ""
    assert source.fetch(limit=1) == [next_row]
    assert database.queries[-1][1]["after"] == first.commit_ts
    assert source.oldest_commit_ts() == next_row.commit_ts


def test_account_refusal_samples_read_filters_in_clickhouse() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("query", "") + request.content.decode())
        return httpx.Response(200, json={"data": []})

    client = OperationalAnalyticsClient(
        base_url="http://clickhouse", user="reader", password="sec" + "ret",
        transport=httpx.MockTransport(handler),
    )
    assert client.account_refusal_samples(
        cutoff="2026-11-20T16:00:00Z", per_route_limit=20, limit=4000,
    ) == []
    [query] = seen
    for clause in ("source IN ('organic', 'synthetic')", "usage_type = 'Credits'",
                   "error_status IN (401, 403)",
                   "route_rank <= {per_route_limit:UInt32}", "LIMIT {limit:UInt32}",
                   "(provider, model) IN ("):
        assert clause in query


def test_gcp_account_refusal_read_uses_clickhouse_directly() -> None:
    calls: list[dict[str, object]] = []

    class FakeAnalytics:
        def account_refusal_samples(
            self, *, cutoff: str, per_route_limit: int, limit: int,
        ) -> list[ProviderBenchmarkSample]:
            calls.append({"cutoff": cutoff, "per_route_limit": per_route_limit, "limit": limit})
            return []

    store = object.__new__(SpannerStore)
    store._operational_analytics = FakeAnalytics()  # type: ignore[assignment]
    assert store.provider_account_refusal_samples(
        cutoff="2026-11-20T16:00:00Z", per_route_limit=20, limit=7,
    ) == []
    assert calls == [{"cutoff": "2026-11-20T16:00:00Z", "per_route_limit": 20, "limit": 7}]


def test_memory_account_refusal_read_filters_and_orders() -> None:
    from trusted_router.storage import InMemoryStore

    store = InMemoryStore()
    base = dict(model="maple/m", provider="maple", provider_name="Maple", status="error",
                usage_type="Credits", streamed=True, error_type="provider_error",
                error_status=403, source="organic")
    for sample_id, created, changes in (
        ("new", "2026-11-20T16:40:00Z", {}),
        ("older", "2026-11-20T16:30:00Z", {"error_status": 401}),
        ("byok", "2026-11-20T16:45:00Z", {"usage_type": "BYOK"}),
        ("probe", "2026-11-20T16:45:00Z", {"source": "synthetic_throughput"}),
        ("gone", "2026-11-20T10:00:00Z", {}),
        ("recovered", "2026-11-20T16:50:00Z", {"status": "success", "error_status": None}),
        ("other-route-ok", "2026-11-20T16:50:00Z",
         {"model": "other/m", "status": "success", "error_status": None}),
    ):
        store.record_provider_benchmark(ProviderBenchmarkSample(
            id=sample_id, created_at=created, **{**base, **changes},  # type: ignore[arg-type]
        ))
    rows = store.provider_account_refusal_samples(
        cutoff="2026-11-20T16:00:00Z", per_route_limit=20, limit=10,
    )
    # A refused route's success comes back (it ends a streak); a never-refused
    # route, BYOK, probes and samples before the cutoff do not.
    assert [row.id for row in rows] == ["recovered", "new", "older"]
    assert [row.id for row in store.provider_account_refusal_samples(
        cutoff="2026-11-20T16:00:00Z", per_route_limit=2, limit=10,
    )] == ["recovered", "new"]
