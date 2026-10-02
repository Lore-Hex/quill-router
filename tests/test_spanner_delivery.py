from __future__ import annotations

import datetime as dt
import json
from typing import Any

from clickhouse.verify_spanner_delivery import verify_delivery
from trusted_router.storage_gcp_operational_analytics_outbox import activity_payload
from trusted_router.storage_models import Generation
from trusted_router.types import UsageType


def _generation(generation_id: str = "gen-delivery-1") -> Generation:
    return Generation(
        id=generation_id,
        request_id="req-delivery-1",
        workspace_id="ws-private",
        key_hash="key-private",
        model="anthropic/claude-haiku-4.5",
        provider="anthropic",
        provider_name="Anthropic",
        app="Synthetic",
        tokens_prompt=10,
        tokens_completion=2,
        total_cost_microdollars=7,
        usage_type=UsageType.CREDITS,
        speed_tokens_per_second=8.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        usage_estimated=False,
        created_at="2026-07-31T12:00:00.000Z",
    )


class FakeSource:
    def __init__(self, generations: list[Generation]) -> None:
        self.generations = generations
        self.calls: list[tuple[dt.datetime, dt.datetime, int]] = []

    def fetch(
        self,
        *,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
    ) -> list[Generation]:
        self.calls.append((start, end, limit))
        return self.generations[:limit]


class FakeClickHouse:
    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.requested_ids: list[str] = []

    def query(
        self,
        sql: str,
        *,
        input_bytes: bytes | None = None,
        external_ids: bool = False,
    ) -> str:
        assert "activity_generations" in sql
        assert external_ids is True
        assert input_bytes is not None
        self.requested_ids = input_bytes.decode().splitlines()
        return "\n".join(
            json.dumps(self.rows[generation_id])
            for generation_id in self.requested_ids
            if generation_id in self.rows
        )


def test_spanner_delivery_matches_content_free_generation_rows() -> None:
    generation = _generation()
    expected = activity_payload(generation)
    source = FakeSource([generation])
    clickhouse = FakeClickHouse({generation.id: expected})
    start = dt.datetime(2026, 7, 31, tzinfo=dt.UTC)
    end = start + dt.timedelta(days=1)

    result = verify_delivery(source, clickhouse, start=start, end=end, limit=100)

    assert result == {
        "sampled": 1,
        "found": 1,
        "missing": 0,
        "mismatched": 0,
        "missing_ids": [],
        "mismatched_ids": [],
        "mismatch_fields": {},
        "ok": True,
    }
    assert source.calls == [(start, end, 100)]
    assert clickhouse.requested_ids == [generation.id]


def test_spanner_delivery_normalizes_integral_float_json_values() -> None:
    generation = _generation()
    generation.speed_tokens_per_second = 1000.0
    actual = activity_payload(generation)
    actual["speed_tokens_per_second"] = 1000

    result = verify_delivery(
        FakeSource([generation]),
        FakeClickHouse({generation.id: actual}),
        start=dt.datetime(2026, 7, 31, tzinfo=dt.UTC),
        end=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        limit=100,
    )

    assert result["ok"] is True
    assert result["mismatch_fields"] == {}


def test_spanner_delivery_matches_clickhouse_defaults_for_legacy_rows() -> None:
    generation = _generation()
    actual = activity_payload(generation)
    actual.update(
        {
            "gateway_request_id": "",
            "synthetic": 0,
            "client_source": "none",
            "client_sdk": "",
            "client_sdk_version": "",
            "client_lang": "",
            "client_runtime": "",
            "client_os": "",
            "client_arch": "",
            "client_prev_outcome": "",
            "client_prev_error_class": "",
            "client_prev_host": "",
        }
    )

    result = verify_delivery(
        FakeSource([generation]),
        FakeClickHouse({generation.id: actual}),
        start=dt.datetime(2026, 7, 31, tzinfo=dt.UTC),
        end=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        limit=100,
    )

    assert result["ok"] is True
    assert result["mismatch_fields"] == {}


def test_spanner_delivery_normalizes_nullable_clickhouse_booleans() -> None:
    generation = _generation()
    generation.client_stream = False
    generation.client_failover_used = True
    actual = activity_payload(generation)
    actual["client_stream"] = 0
    actual["client_failover_used"] = 1

    result = verify_delivery(
        FakeSource([generation]),
        FakeClickHouse({generation.id: actual}),
        start=dt.datetime(2026, 7, 31, tzinfo=dt.UTC),
        end=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        limit=100,
    )

    assert result["ok"] is True
    assert result["mismatch_fields"] == {}


def test_spanner_delivery_reports_missing_and_mismatched_rows() -> None:
    missing = _generation("gen-missing")
    mismatched = _generation("gen-mismatched")
    wrong = activity_payload(mismatched)
    wrong["total_cost_microdollars"] = 999

    result = verify_delivery(
        FakeSource([missing, mismatched]),
        FakeClickHouse({mismatched.id: wrong}),
        start=dt.datetime(2026, 7, 31, tzinfo=dt.UTC),
        end=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        limit=100,
    )

    assert result["ok"] is False
    assert result["missing"] == 1
    assert result["mismatched"] == 1
    assert result["missing_ids"] == [missing.id]
    assert result["mismatched_ids"] == [mismatched.id]
    assert result["mismatch_fields"] == {"total_cost_microdollars": 1}


def test_spanner_delivery_allows_an_empty_quiet_window() -> None:
    result = verify_delivery(
        FakeSource([]),
        FakeClickHouse({}),
        start=dt.datetime(2026, 7, 31, tzinfo=dt.UTC),
        end=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        limit=100,
    )

    assert result["sampled"] == 0
    assert result["ok"] is True
