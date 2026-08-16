from __future__ import annotations

import datetime as dt
from typing import Any

from clickhouse.rollup_client_events import (
    aggregate_client_rollups,
    cap_tenant_requests,
    client_rollup_id,
)

START = dt.datetime(2026, 8, 17, 12, tzinfo=dt.UTC)


def _counter(tenant: str, **updates: Any) -> dict[str, Any]:
    value = {
        "tenant_id": tenant,
        "bucket_start": START.isoformat(),
        "synthetic": 0,
        "sdk": "tr-py",
        "level": "request",
        "endpoint": "responses",
        "host": "apex",
        "outcome": "ok",
        "error_class": "",
        "http_status_class": "2xx",
        "timeout_phase": "none",
        "timeout_floor_met": 1,
        "provider_pinned": 0,
        "requests": 25,
        "attempts": 0,
        "failover_used": 0,
        "first_attempt_success": 25,
        "total_ms_hist": {"lt200": 25},
        "first_event_ms_hist": {"lt100": 25},
        "tr_fault": 0,
    }
    value.update(updates)
    return value


def _coverage(tenant: str, requests: int = 25, synthetic: int = 0) -> dict[str, Any]:
    return {
        "tenant_id": tenant,
        "bucket_start": START.isoformat(),
        "synthetic": synthetic,
        "requests": requests,
    }


def test_tenant_cap_is_25_percent_and_reports_the_excess() -> None:
    allowed, excess = cap_tenant_requests({"large": 80, "small": 20})

    assert allowed == {"large": 25, "small": 20}
    assert excess == 55


def test_rollup_aggregates_distinct_tenants_facets_and_synthetic_policy() -> None:
    rows = [_counter(f"t{index}") for index in range(4)]
    rows.append(_counter("synthetic", synthetic=1, requests=50))
    rows.extend(
        _counter(
            f"t{index}",
            level="attempt",
            requests=0,
            attempts=25,
            first_attempt_success=0,
        )
        for index in range(4)
    )
    coverage = [_coverage(f"t{index}") for index in range(4)]
    coverage.append(_coverage("synthetic", requests=50, synthetic=1))

    result = aggregate_client_rollups(
        rows,
        coverage,
        period="5m",
        period_start=START,
        computed_at=START + dt.timedelta(minutes=1),
    )

    total = next(
        row
        for row in result
        if row["scope"] == "fleet" and row["host"] == row["endpoint"] == row["sdk"] == ""
    )
    host = next(row for row in result if row["scope"] == "fleet" and row["host"] == "apex")
    assert total["requests"] == 100
    assert total["successes"] == 100
    assert total["distinct_tenants"] == 4
    assert total["coverage_requests"] == 100
    assert total["capped_requests"] == 0
    assert host["attempts"] == 100


def test_rollup_id_is_deterministic_over_the_full_key() -> None:
    arguments = {
        "period": "hour",
        "period_start": START,
        "scope": "fleet",
        "tenant_id": "",
        "host": "apex",
        "endpoint": "",
        "sdk": "",
    }

    assert client_rollup_id(**arguments) == client_rollup_id(**arguments)
    assert client_rollup_id(**arguments) != client_rollup_id(**{**arguments, "host": "ally"})


class _Executor:
    """Answers the two fetch queries from memory and records the INSERT."""

    def __init__(self, counters: list[dict[str, Any]], coverage: list[dict[str, Any]]) -> None:
        self._counters = counters
        self._coverage = coverage
        self.queries: list[str] = []
        self.inserted: list[dict[str, Any]] = []

    def query(self, sql: str, *, input_bytes: bytes | None = None) -> bytes:
        import json

        self.queries.append(sql)
        if sql.startswith("INSERT INTO"):
            assert input_bytes is not None
            self.inserted.extend(json.loads(line) for line in input_bytes.decode().splitlines())
            return b""
        rows = self._counters if "client_minute_counters" in sql else self._coverage
        return "\n".join(json.dumps(row) for row in rows).encode()


def test_recompute_covers_six_hours_of_5m_windows_so_late_batches_still_land() -> None:
    """The recompute lookback equals the 6 h late-arrival cap in fetch_inputs.

    A batch that drains 4 h late (control-plane outage, SDK backoff) lands in
    the right minute of client_minute_counters; a 3 h lookback would never
    fold it into that minute's rollup, silently hiding exactly the outage the
    telemetry exists to measure.
    """
    from clickhouse.rollup_client_events import recompute

    now = START + dt.timedelta(hours=5, minutes=30)
    late = _counter("t1", bucket_start=START.isoformat())  # 5.5 h before now
    fresh = _counter("t2", bucket_start=(now - dt.timedelta(minutes=10)).isoformat())
    executor = _Executor([late, fresh], [_coverage("t1"), _coverage("t2")])

    summary = recompute(executor, now=now)  # type: ignore[arg-type]

    assert summary["counter_rows"] == 2
    fetch = executor.queries[0]
    assert "INTERVAL 6 HOUR" in fetch  # late-arrival cap
    five_minute = [row for row in executor.inserted if row["period"] == "5m"]
    starts = {row["period_start"] for row in five_minute}
    assert START.isoformat() in starts  # the 5.5 h-old minute was recomputed
    assert all(row["methodology_version"] == 1 for row in executor.inserted)
    tenant_late = [
        row
        for row in five_minute
        if row["scope"] == "tenant"
        and row["tenant_id"] == "t1"
        and row["host"] == ""
        and row["endpoint"] == ""
        and row["sdk"] == ""
    ]
    assert tenant_late and tenant_late[0]["requests"] == 25
    # Windows without counters are not written (ReplacingMergeTree cannot
    # delete, and counters are append-only), so the empty windows in between
    # produce no rows.
    assert len(starts) == 2


def test_recompute_dry_run_computes_but_never_inserts() -> None:
    from clickhouse.rollup_client_events import recompute

    executor = _Executor([_counter("t1")], [_coverage("t1")])
    summary = recompute(executor, now=START + dt.timedelta(minutes=7), dry_run=True)  # type: ignore[arg-type]

    assert summary["rollups"] > 0
    assert executor.inserted == []
    assert not any(sql.startswith("INSERT INTO") for sql in executor.queries)
