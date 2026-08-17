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
    client_observed_status_section,
    is_excluded,
    tenant_client_reliability_summary,
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


def test_snapshot_fills_canary_freshness_and_private_watch_sections() -> None:
    now = dt.datetime(2026, 8, 17, 12, tzinfo=dt.UTC)
    rows = {
        "watch_15m": [
            _rollup(
                period="5m",
                period_start="2026-08-17T11:55:00Z",
                host="apex",
                attempts=70,
                attempt_tr_fault=2,
                distinct_tenants=4,
                tenant_id="private-one",
            ),
            _rollup(
                period="5m",
                period_start="2026-08-17T11:50:00Z",
                host="apex",
                attempts=80,
                attempt_tr_fault=3,
                distinct_tenants=3,
                tenant_id="private-two",
            ),
            _rollup(
                period="5m",
                period_start="2026-08-17T11:45:00Z",
                host="apex",
                attempts=90,
                attempt_tr_fault=4,
                distinct_tenants=5,
                tenant_id="private-three",
            ),
            _rollup(
                period="5m",
                period_start="2026-08-17T11:40:00Z",
                host="apex",
                attempts=10_000,
                attempt_tr_fault=10_000,
                distinct_tenants=99,
                tenant_id="private-excluded-fourth-row",
            ),
        ],
        "7d": [
            _rollup(
                period="hour",
                period_start="2026-08-17T11:00:00Z",
                host="apex",
                attempts=1_000,
                attempt_tr_fault=10,
                tenant_id="private-four",
            ),
            _rollup(
                period="hour",
                period_start="2026-08-17T10:00:00Z",
                host="apex",
                attempts=500,
                attempt_tr_fault=5,
                tenant_id="private-five",
            ),
        ],
    }

    snapshot = build_client_reliability(
        rows,
        now,
        signals={
            "canary_last_received_at": "2026-08-17 11:50:00",
            "canary_last_24h": 23,
            "newest_received_at": "2026-08-17T11:58:30Z",
        },
    )

    assert snapshot["canary"] == {
        "last_seen_age_seconds": 600,
        "last_24h_count": 23,
    }
    assert snapshot["freshness"] == {
        "newest_received_at": "2026-08-17T11:58:30Z",
        "drain_lag_seconds": None,
        "age_seconds": 90,
    }
    assert snapshot["watch"] == {
        "by_host_15m": {
            "apex": {
                "attempts": 240,
                "attempt_tr_fault": 9,
                "distinct_tenants": 5,
            }
        },
        "by_host_7d": {
            "apex": {
                "attempts": 1_500,
                "attempt_tr_fault": 15,
            }
        },
    }
    encoded = json.dumps(snapshot, sort_keys=True)
    assert "private-" not in encoded


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


def _public_snapshot(**updates: Any) -> dict[str, Any]:
    value = {
        "generated_at": "2026-08-17T12:00:00Z",
        "methodology_version": 1,
        "published": False,
        "windows": {
            name: {
                "requests": 1_000,
                "successes": 990,
                "tr_fault": 10,
                "excluded": 2,
                "aborted": 1,
                "distinct_tenants": 3,
                "coverage": 0.9,
                "p50_total_ms": 100,
                "p95_total_ms": 200,
                "p50_ttft_ms": 100,
                "availability_percent": 99.0,
            }
            for name in ("5m", "1h", "24h", "7d", "30d")
        },
        "by_host_24h": {
            "apex": {"attempts": 100, "attempt_tr_fault": 2, "rate": 0.02}
        },
        "canary": {"last_seen_age_seconds": 10, "last_24h_count": 24},
        "freshness": {"age_seconds": 10},
    }
    value.update(updates)
    return value


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        (None, {"available": False, "reason": "no_data"}),
        (
            _public_snapshot(freshness={"age_seconds": 901}),
            {"available": False, "reason": "stale"},
        ),
    ],
)
def test_client_observed_status_unavailable_table(
    snapshot: dict[str, Any] | None,
    expected: dict[str, Any],
) -> None:
    assert client_observed_status_section(
        snapshot,
        now=dt.datetime(2026, 8, 17, 12, tzinfo=dt.UTC),
    ) == expected


def test_client_observed_status_calibrates_then_passes_percentages() -> None:
    now = dt.datetime(2026, 8, 17, 12, tzinfo=dt.UTC)

    calibrating = client_observed_status_section(_public_snapshot(), now=now)
    published = client_observed_status_section(
        _public_snapshot(published=True),
        now=now,
    )

    assert calibrating["state"] == "calibrating"
    assert calibrating["windows"]["24h"]["availability_percent"] is None
    assert published["state"] == "published"
    assert published["slo_id"] == "client_observed"
    assert published["windows"]["24h"]["availability_percent"] == 99.0


def test_client_observed_status_whitelists_every_nested_field() -> None:
    snapshot = _public_snapshot(
        tenant_id="private-tenant",
        windows={
            "24h": {
                "requests": 100,
                "successes": 99,
                "tr_fault": 1,
                "tenant_id": "private-window-tenant",
            }
        },
        by_host_24h={
            "apex": {
                "attempts": 10,
                "attempt_tr_fault": 1,
                "tenant_id": "private-host-tenant",
            },
            "private-tenant": {"attempts": 1, "attempt_tr_fault": 1},
        },
        canary={
            "last_seen_age_seconds": 10,
            "last_24h_count": 1,
            "tenant_id": "private-canary-tenant",
        },
    )

    section = client_observed_status_section(
        snapshot,
        now=dt.datetime(2026, 8, 17, 12, tzinfo=dt.UTC),
    )

    encoded = json.dumps(section, sort_keys=True)
    assert "tenant_id" not in encoded
    for private_value in (
        "private-tenant",
        "private-window-tenant",
        "private-host-tenant",
        "private-canary-tenant",
    ):
        assert private_value not in encoded


def test_tenant_summary_uses_one_rollup_granularity_and_exact_counts() -> None:
    rows = [
        _rollup(
            period="5m",
            requests=100,
            successes=98,
            tr_fault_failures=2,
            attempts=110,
            failover_used=4,
            first_attempt_success=92,
        ),
        _rollup(
            period="5m",
            host="apex",
            requests=0,
            attempts=110,
            attempt_tr_fault=3,
        ),
        _rollup(period="hour", requests=10_000),
    ]

    summary = tenant_client_reliability_summary(rows, window_minutes=60)

    assert summary["requests"] == 100
    assert summary["successes"] == 98
    assert summary["tr_fault"] == 2
    assert summary["attempts"] == 110
    assert summary["failover_used"] == 4
    assert summary["first_attempt_success"] == 92
    assert summary["by_host"]["apex"] == {
        "attempts": 110,
        "attempt_tr_fault": 3,
        "rate": 0.027273,
    }
