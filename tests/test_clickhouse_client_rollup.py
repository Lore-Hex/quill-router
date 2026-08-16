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
