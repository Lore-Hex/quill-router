"""Pure methodology for client-observed reliability telemetry."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

METHODOLOGY_VERSION = 1

HOSTS = (
    "apex",
    "ally",
    "uptime",
    "us_central1",
    "us_east4",
    "europe_west4",
    "control",
    "custom",
)
ENDPOINTS = (
    "chat_completions",
    "messages",
    "responses",
    "embeddings",
    "images",
    "videos",
    "models",
    "fusion",
    "control_other",
    "inference_other",
)
OUTCOMES = (
    "ok",
    "http_error",
    "transport_error",
    "timeout",
    "stream_broken",
    "aborted",
)
FINAL_OUTCOMES = (*OUTCOMES, "exhausted")
ERROR_CLASSES = (
    "dns",
    "tls",
    "connect_refused",
    "connect_timeout",
    "connect_error",
    "read_timeout",
    "write_timeout",
    "pool_timeout",
    "protocol_error",
    "reset",
    "io_error",
    "proxy_error",
    "stream_stalled",
    "unknown",
)
TIMEOUT_PHASES = ("none", "connect", "first_byte", "idle", "total")
LATENCY_BUCKETS = (
    "lt100",
    "lt200",
    "lt400",
    "lt800",
    "lt1600",
    "lt3200",
    "lt6400",
    "lt12800",
    "lt25600",
    "lt51200",
    "lt102400",
    "ge102400",
)

# Singular aliases make the names in the cross-repo contract executable while
# the plural forms remain convenient for parity tests in every SDK.
Host = HOSTS
Endpoint = ENDPOINTS
Outcome = OUTCOMES
FinalOutcome = FINAL_OUTCOMES
ErrorClass = ERROR_CLASSES
TimeoutPhase = TIMEOUT_PHASES
LatencyBucket = LATENCY_BUCKETS

TR_FAULT_TRANSPORT_CLASSES = (
    "dns",
    "tls",
    "connect_refused",
    "connect_timeout",
    "connect_error",
    "reset",
    "io_error",
    "protocol_error",
)


def _status_class(value: str | int | None) -> str:
    if value is None:
        return "none"
    if isinstance(value, int):
        if value == 429:
            return "429"
        if 200 <= value <= 299:
            return "2xx"
        if 400 <= value <= 499:
            return "4xx"
        if 500 <= value <= 599:
            return "5xx"
        return "none"
    return value.casefold()


def is_excluded(
    *,
    level: str,
    outcome: str,
    error_class: str | None,
    error_source: str | None,
    http_status_class_or_status: str | int | None,
    host: str,
    provider_pinned: bool,
    timeout_phase: str,
    timeout_floor_met: bool,
) -> bool:
    """Return whether the observation is disclosed outside the denominator."""

    if level not in {"attempt", "request"}:
        raise ValueError(f"unsupported client reliability level: {level}")
    status_class = _status_class(http_status_class_or_status)
    error_class = error_class or ""
    if host == "custom" or outcome == "aborted":
        return True
    if status_class in {"4xx", "429"}:
        return True
    if error_class in {"pool_timeout", "proxy_error"}:
        return True
    if timeout_phase == "total":
        return True
    if outcome == "timeout" and not timeout_floor_met:
        return True
    return bool(
        outcome == "http_error"
        and status_class == "5xx"
        and provider_pinned
        and error_source == "provider"
    )


def classify_tr_fault(
    *,
    level: str,
    outcome: str,
    error_class: str | None,
    error_source: str | None,
    http_status_class_or_status: str | int | None,
    host: str,
    provider_pinned: bool,
    timeout_phase: str,
    timeout_floor_met: bool,
) -> bool:
    """Apply client-observed availability methodology version 1 exactly."""

    if is_excluded(
        level=level,
        outcome=outcome,
        error_class=error_class,
        error_source=error_source,
        http_status_class_or_status=http_status_class_or_status,
        host=host,
        provider_pinned=provider_pinned,
        timeout_phase=timeout_phase,
        timeout_floor_met=timeout_floor_met,
    ):
        return False
    error_class = error_class or ""
    status_class = _status_class(http_status_class_or_status)
    if outcome == "transport_error" and error_class in TR_FAULT_TRANSPORT_CLASSES:
        return True
    if outcome == "http_error" and status_class == "5xx":
        return True
    if outcome == "timeout" and timeout_phase in {"connect", "first_byte"} and timeout_floor_met:
        return True
    if outcome == "stream_broken":
        return True
    if error_class == "stream_stalled" and timeout_floor_met:
        return True
    return error_class == "unknown"


def availability(successes: int, tr_fault: int) -> float | None:
    denominator = successes + tr_fault
    return successes / denominator if denominator else None


def _int(row: Mapping[str, Any], key: str) -> int:
    return int(row.get(key) or 0)


def _merge_histograms(
    rows: Iterable[Mapping[str, Any]],
    key: str,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        histogram = row.get(key) or {}
        if not isinstance(histogram, Mapping):
            continue
        for bucket, count in histogram.items():
            result[str(bucket)] = result.get(str(bucket), 0) + int(count)
    return result


def _histogram_percentile(histogram: Mapping[str, int], percentile: int) -> int | None:
    total = sum(max(0, int(histogram.get(bucket, 0))) for bucket in LATENCY_BUCKETS)
    if total <= 0:
        return None
    target = max(1, math.ceil(total * percentile / 100))
    seen = 0
    for bucket in LATENCY_BUCKETS:
        seen += max(0, int(histogram.get(bucket, 0)))
        if seen >= target:
            return 102_400 if bucket == "ge102400" else int(bucket[2:])
    return 102_400


def _total_rows(rows: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    totals = [
        row
        for row in rows
        if not str(row.get("host") or "")
        and not str(row.get("endpoint") or "")
        and not str(row.get("sdk") or "")
    ]
    return totals or list(rows)


def _window(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    rows = _total_rows(rows)
    requests = sum(_int(row, "requests") for row in rows)
    successes = sum(_int(row, "successes") for row in rows)
    tr_fault = sum(_int(row, "tr_fault_failures") for row in rows)
    distinct_tenants = max((_int(row, "distinct_tenants") for row in rows), default=0)
    coverage_requests = sum(_int(row, "coverage_requests") for row in rows)
    measured = availability(successes, tr_fault)
    total_hist = _merge_histograms(rows, "total_ms_hist")
    first_event_hist = _merge_histograms(rows, "first_event_ms_hist")
    return {
        "requests": requests,
        "successes": successes,
        "tr_fault": tr_fault,
        "excluded": sum(_int(row, "excluded_failures") for row in rows),
        "aborted": sum(_int(row, "aborted") for row in rows),
        "availability_percent": (
            round(measured * 100, 4)
            if measured is not None and requests >= 1_000 and distinct_tenants >= 3
            else None
        ),
        "distinct_tenants": distinct_tenants,
        "coverage": (round(successes / coverage_requests, 4) if coverage_requests else None),
        "p50_total_ms": _histogram_percentile(total_hist, 50),
        "p95_total_ms": _histogram_percentile(total_hist, 95),
        "p50_ttft_ms": _histogram_percentile(first_event_hist, 50),
    }


def _host_breakdown(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        host = str(row.get("host") or "")
        if not host or row.get("endpoint") or row.get("sdk"):
            continue
        item = result.setdefault(host, {"attempts": 0, "attempt_tr_fault": 0})
        item["attempts"] += _int(row, "attempts")
        item["attempt_tr_fault"] += _int(row, "attempt_tr_fault")
    for item in result.values():
        attempts = int(item["attempts"])
        item["rate"] = round(int(item["attempt_tr_fault"]) / attempts, 6) if attempts else None
    return result


def _sdk_breakdown(rows: Sequence[Mapping[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        sdk = str(row.get("sdk") or "")
        if not sdk or row.get("host") or row.get("endpoint"):
            continue
        result[sdk] = result.get(sdk, 0) + _int(row, "requests")
    return result


def build_client_reliability(
    rows_by_window: Mapping[str, Iterable[Mapping[str, Any]]],
    now: dt.datetime,
) -> dict[str, Any]:
    """Build the content-free public snapshot from bounded fleet rollups."""

    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    else:
        now = now.astimezone(dt.UTC)
    windows = {
        name: [dict(row) for row in rows_by_window.get(name, ())]
        for name in ("5m", "1h", "24h", "7d", "30d")
    }
    return {
        "generated_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "methodology_version": METHODOLOGY_VERSION,
        "published": False,
        "windows": {name: _window(rows) for name, rows in windows.items()},
        "by_host_24h": _host_breakdown(windows["24h"]),
        "by_sdk_24h": _sdk_breakdown(windows["24h"]),
        "canary": {"last_seen_age_seconds": None, "last_24h_count": 0},
        "freshness": {"newest_received_at": None, "drain_lag_seconds": None},
    }
