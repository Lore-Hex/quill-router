from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any

import pytest

from clickhouse.operational_fingerprint import clickhouse_rows, parse_utc
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


# G6 in docs/design/clickhouse-high-availability.md: the half-hourly check
# looked rows up by generation_id alone, which is not a prefix of the sort key
# (tenant_id, created_at, generation_id), so every run read the whole table
# with FINAL. It now bounds the lookup by the sampled rows' created_at span.

_BOUND = re.compile(
    r"AND created_at >= toDateTime64\('([^']+)', 3, 'UTC'\) "
    r"AND created_at <= toDateTime64\('([^']+)', 3, 'UTC'\) "
)


def _ch_instant(text: str) -> dt.datetime:
    return dt.datetime.fromisoformat(text.replace(" ", "T")).replace(tzinfo=dt.UTC)


class WindowedClickHouse:
    """Applies the created_at bound the way ClickHouse would, and records calls."""

    def __init__(self, rows: dict[str, dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[list[str], tuple[dt.datetime, dt.datetime] | None]] = []

    def query(
        self,
        sql: str,
        *,
        input_bytes: bytes | None = None,
        external_ids: bool = False,
    ) -> str:
        assert "FROM activity_generations FINAL" in sql
        assert external_ids is True and input_bytes is not None
        ids = input_bytes.decode().splitlines()
        match = _BOUND.search(sql)
        window = (_ch_instant(match[1]), _ch_instant(match[2])) if match else None
        self.calls.append((ids, window))
        lines = []
        for generation_id in ids:
            row = self.rows.get(generation_id)
            if row is None:
                continue
            created = parse_utc(row["created_at"])
            assert created is not None
            if window is not None and not window[0] <= created <= window[1]:
                continue
            lines.append(json.dumps(row))
        return "\n".join(lines)


def _at(generation_id: str, created_at: str) -> Generation:
    generation = _generation(generation_id)
    generation.created_at = created_at
    return generation


WINDOW = (dt.datetime(2026, 7, 31, tzinfo=dt.UTC), dt.datetime(2026, 8, 1, tzinfo=dt.UTC))


def test_spanner_delivery_bounds_the_lookup_by_the_sampled_created_at_span() -> None:
    early = _at("gen-early", "2026-07-31T01:02:03.004Z")
    late = _at("gen-late", "2026-07-31T22:00:00.999Z")
    clickhouse = WindowedClickHouse(
        {g.id: activity_payload(g) for g in (early, late)}
    )

    result = verify_delivery(
        FakeSource([late, early]), clickhouse, start=WINDOW[0], end=WINDOW[1], limit=100
    )

    assert result["ok"] is True and result["found"] == 2
    # One bounded query covering exactly the sampled span; no unbounded scan.
    assert clickhouse.calls == [
        (
            ["gen-late", "gen-early"],
            (
                dt.datetime(2026, 7, 31, 1, 2, 3, 4000, tzinfo=dt.UTC),
                dt.datetime(2026, 7, 31, 22, 0, 0, 999000, tzinfo=dt.UTC),
            ),
        )
    ]


def test_a_row_stored_with_another_created_at_is_mismatched_not_missing() -> None:
    sampled = _at("gen-shifted", "2026-07-31T12:00:00.000Z")
    neighbour = _at("gen-neighbour", "2026-07-31T13:00:00.000Z")
    stored = activity_payload(sampled)
    stored["created_at"] = "2026-07-30T12:00:00.000Z"  # outside the bounded span
    clickhouse = WindowedClickHouse({sampled.id: stored, neighbour.id: activity_payload(neighbour)})

    result = verify_delivery(
        FakeSource([sampled, neighbour]), clickhouse, start=WINDOW[0], end=WINDOW[1], limit=100
    )

    assert result["missing"] == 0
    assert result["mismatched_ids"] == ["gen-shifted"]
    assert result["mismatch_fields"] == {"created_at": 1}
    # The unbounded lookup covers only the ID the bounded one did not find.
    assert [ids for ids, _ in clickhouse.calls] == [
        ["gen-shifted", "gen-neighbour"],
        ["gen-shifted"],
    ]
    assert clickhouse.calls[1][1] is None


def test_a_row_absent_from_clickhouse_is_still_reported_missing() -> None:
    present = _at("gen-present", "2026-07-31T12:00:00.000Z")
    absent = _at("gen-absent", "2026-07-31T12:30:00.000Z")
    clickhouse = WindowedClickHouse({present.id: activity_payload(present)})

    result = verify_delivery(
        FakeSource([present, absent]), clickhouse, start=WINDOW[0], end=WINDOW[1], limit=100
    )

    assert result["ok"] is False
    assert result["missing_ids"] == ["gen-absent"]
    assert [window is None for _, window in clickhouse.calls] == [False, True]


def test_an_unparseable_created_at_falls_back_to_one_unbounded_lookup() -> None:
    odd = _at("gen-odd", "not-a-timestamp")
    clickhouse = WindowedClickHouse({})

    verify_delivery(FakeSource([odd]), clickhouse, start=WINDOW[0], end=WINDOW[1], limit=100)

    assert clickhouse.calls == [(["gen-odd"], None)]


def test_bound_literals_round_outward_to_whole_milliseconds() -> None:
    low = dt.datetime(2026, 7, 31, 1, 2, 3, 4567, tzinfo=dt.UTC)
    high = dt.datetime(2026, 7, 31, 1, 2, 3, 4001, tzinfo=dt.UTC)
    clickhouse = WindowedClickHouse({})

    clickhouse_rows(
        clickhouse,
        table="activity_generations",
        id_column="generation_id",
        ids=["gen-a"],
        created_at_range=(low, high + dt.timedelta(seconds=1)),
    )

    assert clickhouse.calls[0][1] == (
        dt.datetime(2026, 7, 31, 1, 2, 3, 4000, tzinfo=dt.UTC),
        dt.datetime(2026, 7, 31, 1, 2, 4, 5000, tzinfo=dt.UTC),
    )


def test_bound_literals_convert_other_timezones_to_utc() -> None:
    berlin = dt.timezone(dt.timedelta(hours=2))
    clickhouse = WindowedClickHouse({})

    clickhouse_rows(
        clickhouse,
        table="activity_generations",
        id_column="generation_id",
        ids=["gen-a"],
        created_at_range=(
            dt.datetime(2026, 7, 31, 14, tzinfo=berlin),
            dt.datetime(2026, 7, 31, 15, tzinfo=berlin),
        ),
    )

    assert clickhouse.calls[0][1] == (
        dt.datetime(2026, 7, 31, 12, tzinfo=dt.UTC),
        dt.datetime(2026, 7, 31, 13, tzinfo=dt.UTC),
    )


@pytest.mark.parametrize(
    ("table", "id_column", "window", "message"),
    [
        ("provider_benchmark_samples", "id", WINDOW, "activity_generations only"),
        ("activity_generations", "generation_id", (WINDOW[1], WINDOW[0]), "ends before"),
        (
            "activity_generations",
            "generation_id",
            (dt.datetime(2026, 7, 31), dt.datetime(2026, 8, 1)),
            "timezone-aware",
        ),
    ],
)
def test_bound_rejects_unsupported_tables_and_malformed_windows(
    table: str, id_column: str, window: tuple[dt.datetime, dt.datetime], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        clickhouse_rows(
            WindowedClickHouse({}),
            table=table,
            id_column=id_column,
            ids=["gen-a"],
            created_at_range=window,
        )
