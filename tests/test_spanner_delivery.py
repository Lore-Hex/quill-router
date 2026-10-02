from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
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
        "duplicated": 0,
        "missing_ids": [],
        "mismatched_ids": [],
        "duplicated_ids": [],
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


# G6 in docs/design/clickhouse-high-availability.md: the lookup reads every
# stored copy of each generation (through the by_generation_id projection), so a
# generation stored under two sort keys is reported instead of hidden behind
# whichever row FINAL returned last.


class MultiRowClickHouse:
    def __init__(self, rows: dict[str, list[dict[str, Any]]]) -> None:
        self.rows = rows
        self.sql: list[str] = []

    def query(
        self,
        sql: str,
        *,
        input_bytes: bytes | None = None,
        external_ids: bool = False,
    ) -> str:
        assert external_ids is True and input_bytes is not None
        self.sql.append(sql)
        lines = []
        for generation_id in input_bytes.decode().splitlines():
            lines.extend(json.dumps(row) for row in self.rows.get(generation_id, []))
        return "\n".join(lines)


def test_a_generation_stored_under_two_sort_keys_is_reported_as_duplicated() -> None:
    twice = _generation("gen-twice")
    once = _generation("gen-once")
    shifted = activity_payload(twice)
    shifted["created_at"] = "2026-07-30T08:00:00.000Z"
    clickhouse = MultiRowClickHouse(
        {
            twice.id: [activity_payload(twice), shifted],
            once.id: [activity_payload(once)],
        }
    )

    result = verify_delivery(
        FakeSource([twice, once]),
        clickhouse,
        start=dt.datetime(2026, 7, 31, tzinfo=dt.UTC),
        end=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        limit=100,
    )

    assert result["ok"] is False
    assert result["duplicated_ids"] == ["gen-twice"]
    assert result["duplicated"] == 1
    # It is neither missing nor compared as a mismatch; the other row passes.
    assert result["missing"] == 0 and result["mismatched"] == 0
    assert result["found"] == 2


def test_the_lookup_finds_sort_keys_by_generation_then_reads_rows_by_key() -> None:
    generation = _generation()
    clickhouse = MultiRowClickHouse({generation.id: [activity_payload(generation)]})

    verify_delivery(
        FakeSource([generation]),
        clickhouse,
        start=dt.datetime(2026, 7, 31, tzinfo=dt.UTC),
        end=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
        limit=100,
    )

    [sql] = clickhouse.sql
    # The outer read is by the full sort key, so the primary index serves it;
    # the inner lookup is what the by_generation_id projection serves.
    assert "FROM activity_generations FINAL WHERE (tenant_id, created_at, generation_id) IN (" in sql
    assert "SELECT tenant_id, created_at, generation_id FROM activity_generations WHERE generation_id IN (SELECT id FROM wanted)" in sql


def test_the_projection_migration_matches_the_lookup() -> None:
    migration = (
        Path(__file__).resolve().parents[1]
        / "clickhouse/023_activity_generation_id_projection_replicated.sql"
    ).read_text()
    assert "MODIFY SETTING deduplicate_merge_projection_mode = 'rebuild'" in migration
    assert (
        "ADD PROJECTION IF NOT EXISTS by_generation_id\n"
        "    (SELECT generation_id, tenant_id, created_at ORDER BY generation_id)"
    ) in migration
    assert "MATERIALIZE PROJECTION by_generation_id" in migration
    # The replicated-migration applier only accepts ON CLUSTER statements.
    statements = [line for line in migration.splitlines() if line.startswith("ALTER TABLE")]
    assert statements and all(
        line == "ALTER TABLE tr.activity_generations ON CLUSTER trustedrouter" for line in statements
    )
