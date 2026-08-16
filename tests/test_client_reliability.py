from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from trusted_router.client_reliability import (
    METHODOLOGY_VERSION,
    availability,
    build_client_reliability,
    classify_tr_fault,
    is_excluded,
    timeout_floor_met,
)


def _facts(**updates: Any) -> dict[str, Any]:
    value = {
        "level": "request",
        "outcome": "transport_error",
        "error_class": "dns",
        "error_source": "unknown",
        "http_status_class_or_status": "none",
        "host": "apex",
        "provider_pinned": False,
        "timeout_phase": "none",
        "timeout_floor_met": True,
    }
    value.update(updates)
    return value


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"error_class": "dns"}, True),
        ({"error_class": "tls"}, True),
        ({"error_class": "connect_refused"}, True),
        ({"error_class": "connect_timeout"}, True),
        ({"error_class": "connect_error"}, True),
        ({"error_class": "reset"}, True),
        ({"error_class": "io_error"}, True),
        ({"error_class": "protocol_error"}, True),
        ({"host": "custom"}, False),
        ({"error_class": "pool_timeout"}, False),
        ({"error_class": "proxy_error"}, False),
        (
            {
                "outcome": "http_error",
                "error_class": None,
                "http_status_class_or_status": 503,
            },
            True,
        ),
        (
            {
                "outcome": "http_error",
                "error_class": None,
                "http_status_class_or_status": "5xx",
                "provider_pinned": True,
                "error_source": "provider",
            },
            False,
        ),
        (
            {
                "outcome": "timeout",
                "error_class": "read_timeout",
                "timeout_phase": "connect",
                "timeout_floor_met": True,
            },
            True,
        ),
        (
            {
                "outcome": "timeout",
                "error_class": "read_timeout",
                "timeout_phase": "first_byte",
                "timeout_floor_met": False,
            },
            False,
        ),
        (
            {
                "outcome": "timeout",
                "error_class": "read_timeout",
                "timeout_phase": "total",
            },
            False,
        ),
        ({"outcome": "stream_broken", "error_class": None}, True),
        ({"error_class": "stream_stalled", "timeout_floor_met": True}, True),
        ({"error_class": "stream_stalled", "timeout_floor_met": False}, False),
        ({"error_class": "unknown"}, True),
        ({"outcome": "aborted", "error_class": "unknown"}, False),
        (
            {
                "outcome": "http_error",
                "error_class": None,
                "http_status_class_or_status": "4xx",
            },
            False,
        ),
        (
            {
                "outcome": "http_error",
                "error_class": None,
                "http_status_class_or_status": "429",
            },
            False,
        ),
    ],
)
def test_methodology_v1_golden_table(updates: dict[str, Any], expected: bool) -> None:
    assert classify_tr_fault(**_facts(**updates)) is expected


def test_disclosed_exclusions_cover_every_methodology_branch() -> None:
    assert is_excluded(**_facts(host="custom"))
    assert is_excluded(**_facts(outcome="aborted"))
    assert is_excluded(
        **_facts(
            outcome="http_error",
            error_class=None,
            http_status_class_or_status="429",
        )
    )
    assert is_excluded(**_facts(error_class="pool_timeout"))
    assert is_excluded(
        **_facts(
            outcome="timeout",
            timeout_phase="first_byte",
            timeout_floor_met=False,
        )
    )


def _rollup(**updates: Any) -> dict[str, Any]:
    value = {
        "host": "",
        "endpoint": "",
        "sdk": "",
        "requests": 1_000,
        "successes": 990,
        "tr_fault_failures": 10,
        "excluded_failures": 5,
        "aborted": 1,
        "attempts": 1_010,
        "attempt_tr_fault": 4,
        "distinct_tenants": 3,
        "coverage_requests": 1_100,
        "total_ms_hist": {"lt100": 500, "lt200": 450, "lt400": 50},
        "first_event_ms_hist": {"lt100": 700, "lt200": 300},
    }
    value.update(updates)
    return value


def test_snapshot_gate_privacy_and_histogram_percentiles() -> None:
    rows = {
        "5m": [_rollup(requests=999)],
        "1h": [_rollup(distinct_tenants=2)],
        "24h": [
            _rollup(tenant_id="private-tenant"),
            _rollup(host="apex", attempts=100, attempt_tr_fault=2, requests=0),
            _rollup(sdk="tr-py", requests=750),
        ],
        "7d": [],
        "30d": [],
    }

    snapshot = build_client_reliability(
        rows,
        dt.datetime(2026, 8, 17, 12, tzinfo=dt.UTC),
    )

    assert snapshot["methodology_version"] == METHODOLOGY_VERSION
    assert snapshot["published"] is False
    assert snapshot["windows"]["5m"]["availability_percent"] is None
    assert snapshot["windows"]["1h"]["availability_percent"] is None
    assert snapshot["windows"]["24h"]["availability_percent"] == 99.0
    assert snapshot["windows"]["24h"]["p50_total_ms"] == 100
    assert snapshot["windows"]["24h"]["p95_total_ms"] == 200
    assert snapshot["windows"]["24h"]["p50_ttft_ms"] == 100
    assert snapshot["by_host_24h"]["apex"] == {
        "attempts": 100,
        "attempt_tr_fault": 2,
        "rate": 0.02,
    }
    assert snapshot["by_sdk_24h"] == {"tr-py": 750}
    assert "private-tenant" not in json.dumps(snapshot, sort_keys=True)


def test_availability_has_an_empty_denominator_sentinel() -> None:
    assert availability(99, 1) == 0.99
    assert availability(0, 0) is None


@pytest.mark.parametrize(
    ("phase", "configured_ms", "expected"),
    [
        ("connect", 9_999, False),
        ("connect", 10_000, True),
        ("first_byte", 59_999, False),
        ("first_byte", 60_000, True),
        ("idle", 29_999, False),
        ("idle", 30_000, True),
        ("total", 3_600_000, False),
        ("none", 3_600_000, False),
        ("connect", None, False),
    ],
)
def test_timeout_floor_met_table(
    phase: str,
    configured_ms: int | None,
    expected: bool,
) -> None:
    assert timeout_floor_met(phase, configured_ms) is expected
