from __future__ import annotations

import dataclasses
import datetime as dt
import json
import threading
from typing import Any

import httpx
import pytest

from clickhouse.ingest_operational_outbox import (
    CanonicalOperationalEvent,
    OperationalOutboxRow,
    drain_once,
    normalise_operational_event,
)
from clickhouse.rollup_synthetic import (
    build_raw_rollups,
    complete_window_rollups,
    monthly_from_daily,
)
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


def test_activity_payload_uses_surrogates_and_omits_content_and_raw_ids() -> None:
    generation = _generation()
    payload = activity_payload(generation)
    encoded = json.dumps(payload, sort_keys=True)

    assert payload["tenant_id"] == analytics_surrogate(
        "workspace", generation.workspace_id
    )
    assert payload["key_id"] == analytics_surrogate("api-key", generation.key_hash)
    assert generation.workspace_id not in encoded
    assert generation.key_hash not in encoded
    assert "private model output" not in encoded
    assert "tool_calls" not in payload
    assert "operator_cost_microdollars" not in payload
    assert "prompt_content" not in payload
    assert "output_content" not in payload


def test_operational_outbox_enqueue_is_sharded_and_commit_timestamped() -> None:
    database = _Database()
    generation = _generation()
    SpannerOperationalAnalyticsOutbox(database, _ParamTypes()).enqueue_activity(
        generation
    )

    [(sql, params, param_types)] = database.transaction.calls
    assert "PENDING_COMMIT_TIMESTAMP()" in sql
    assert params["event_kind"] == "activity"
    assert params["event_id"] == generation.id
    assert params["shard"] == operational_analytics_shard(
        f"activity:{generation.id}"
    )
    assert json.loads(params["payload"])["tenant_id"] != generation.workspace_id
    assert param_types == {
        "shard": "INT64",
        "event_kind": "STRING",
        "event_id": "STRING",
        "payload": "STRING",
    }


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


def test_public_snapshot_reads_newest_revision_across_month_partitions() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        sql = request.content.decode()
        assert "WHERE name = {name:String}" in sql
        assert "ORDER BY generated_at DESC" in sql
        assert request.url.params["param_name"] == "leaderboard"
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

    assert client.public_snapshot("leaderboard") == {
        "generated_at": "2026-08-01T00:00:00Z"
    }


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
    event = normalise_operational_event(_outbox_row())
    assert event.event_kind == "activity"
    assert event.row["generation_id"] == _generation().id
    assert event.row["ingest_version"].startswith("2026-07-31T12:35:00")

    with pytest.raises(ValueError, match="unsupported operational event kind"):
        normalise_operational_event(
            dataclasses.replace(_outbox_row(), event_kind="prompt")
        )


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

    first = build_raw_rollups(samples, periods={"hour", "day"})
    second = build_raw_rollups(samples, periods={"hour", "day"})
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
