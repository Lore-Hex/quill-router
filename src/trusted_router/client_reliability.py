"""Pure methodology for client-observed reliability telemetry."""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

METHODOLOGY_VERSION = 1
WINDOW_NAMES = ("5m", "1h", "24h", "7d", "30d")
#: Fixed public disclosure carried on ``client_observed.all_traffic``.
ALL_TRAFFIC_NOTE = (
    "Calibration view; includes synthetic canary traffic; uncapped; ungated; "
    "not the published methodology."
)

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
SDKS = ("tr-py", "tr-js", "tr-go", "tr-rust", "tr-java", "tr-swift")
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


def timeout_floor_met(timeout_phase: str, configured_timeout_ms: int | None) -> bool:
    """Return whether a client timeout met methodology v1's disclosed floor."""
    floors = {
        "connect": 10_000,
        "first_byte": 60_000,
        "idle": 30_000,
    }
    floor = floors.get(timeout_phase)
    return bool(
        floor is not None
        and configured_timeout_ms is not None
        and configured_timeout_ms >= floor
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


def histogram_percentile(histogram: Mapping[str, int], percentile: int) -> int | None:
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


def _window(rows: Sequence[Mapping[str, Any]], *, gated: bool = True) -> dict[str, Any]:
    """Summarize one window; ``gated`` applies the publication thresholds."""

    rows = _total_rows(rows)
    requests = sum(_int(row, "requests") for row in rows)
    successes = sum(_int(row, "successes") for row in rows)
    tr_fault = sum(_int(row, "tr_fault_failures") for row in rows)
    distinct_tenants = max((_int(row, "distinct_tenants") for row in rows), default=0)
    coverage_requests = sum(_int(row, "coverage_requests") for row in rows)
    measured = availability(successes, tr_fault)
    total_hist = _merge_histograms(rows, "total_ms_hist")
    first_event_hist = _merge_histograms(rows, "first_event_ms_hist")
    availability_percent: float | None = None
    if measured is not None and (not gated or (requests >= 1_000 and distinct_tenants >= 3)):
        availability_percent = round(measured * 100, 4)
    return {
        "requests": requests,
        "successes": successes,
        "tr_fault": tr_fault,
        "excluded": sum(_int(row, "excluded_failures") for row in rows),
        "aborted": sum(_int(row, "aborted") for row in rows),
        "availability_percent": availability_percent,
        "distinct_tenants": distinct_tenants,
        "coverage": (round(successes / coverage_requests, 4) if coverage_requests else None),
        "p50_total_ms": histogram_percentile(total_hist, 50),
        "p95_total_ms": histogram_percentile(total_hist, 95),
        "p50_ttft_ms": histogram_percentile(first_event_hist, 50),
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


def _parse_timestamp(value: Any) -> dt.datetime | None:
    if value in (None, ""):
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    parsed = parsed.astimezone(dt.UTC)
    # ClickHouse aggregate functions return the type's epoch default when no
    # row matches. That sentinel means "not seen", not a decades-stale event.
    return parsed if parsed > dt.datetime(1970, 1, 1, tzinfo=dt.UTC) else None


def _age_seconds(now: dt.datetime, value: dt.datetime | None) -> int | None:
    if value is None:
        return None
    return max(0, int((now - value).total_seconds()))


def _host_facet(row: Mapping[str, Any]) -> str | None:
    host = str(row.get("host") or "")
    if not host or row.get("endpoint") or row.get("sdk"):
        return None
    return host


def _watch_15m(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    by_host: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("period") not in (None, "", "5m"):
            continue
        host = _host_facet(row)
        if host is not None:
            by_host.setdefault(host, []).append(row)

    result: dict[str, dict[str, int]] = {}
    for host, host_rows in sorted(by_host.items()):
        recent = sorted(
            host_rows,
            key=lambda row: _parse_timestamp(row.get("period_start"))
            or dt.datetime.min.replace(tzinfo=dt.UTC),
            reverse=True,
        )[:3]
        result[host] = {
            "attempts": sum(_int(row, "attempts") for row in recent),
            "attempt_tr_fault": sum(_int(row, "attempt_tr_fault") for row in recent),
            "distinct_tenants": max(
                (_int(row, "distinct_tenants") for row in recent),
                default=0,
            ),
        }
    return result


def _watch_7d(rows: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        if row.get("period") not in (None, "", "hour"):
            continue
        host = _host_facet(row)
        if host is None:
            continue
        item = result.setdefault(host, {"attempts": 0, "attempt_tr_fault": 0})
        item["attempts"] += _int(row, "attempts")
        item["attempt_tr_fault"] += _int(row, "attempt_tr_fault")
    return dict(sorted(result.items()))


def tenant_client_reliability_summary(
    rows: Iterable[Mapping[str, Any]],
    *,
    window_minutes: int,
) -> dict[str, Any]:
    """Summarize tenant rollups without applying the fleet publication gate."""

    period = "5m" if window_minutes <= 360 else "hour"
    selected = [
        dict(row)
        for row in rows
        if not row.get("period") or str(row.get("period")) == period
    ]
    totals = _total_rows(selected)
    total_hist = _merge_histograms(totals, "total_ms_hist")
    first_event_hist = _merge_histograms(totals, "first_event_ms_hist")
    return {
        "requests": sum(_int(row, "requests") for row in totals),
        "successes": sum(_int(row, "successes") for row in totals),
        "tr_fault": sum(_int(row, "tr_fault_failures") for row in totals),
        "excluded": sum(_int(row, "excluded_failures") for row in totals),
        "aborted": sum(_int(row, "aborted") for row in totals),
        "attempts": sum(_int(row, "attempts") for row in totals),
        "failover_used": sum(_int(row, "failover_used") for row in totals),
        "first_attempt_success": sum(
            _int(row, "first_attempt_success") for row in totals
        ),
        "p50_total_ms": histogram_percentile(total_hist, 50),
        "p95_total_ms": histogram_percentile(total_hist, 95),
        "p50_ttft_ms": histogram_percentile(first_event_hist, 50),
        "by_host": _host_breakdown(selected),
    }


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _snapshot_age_seconds(
    snapshot: Mapping[str, Any],
    *,
    now: dt.datetime,
) -> float | None:
    freshness = snapshot.get("freshness")
    if isinstance(freshness, Mapping):
        age_seconds = _optional_float(freshness.get("age_seconds"))
        if age_seconds is not None:
            return age_seconds if age_seconds >= 0 else None
    generated_at = snapshot.get("generated_at")
    if not isinstance(generated_at, str):
        return None
    try:
        generated = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=dt.UTC)
    age_seconds = (now - generated.astimezone(dt.UTC)).total_seconds()
    return age_seconds if age_seconds >= 0 else None


def _status_window(window: Any, *, published: bool) -> dict[str, Any]:
    row = window if isinstance(window, Mapping) else {}
    return {
        "requests": max(0, _int(row, "requests")),
        "successes": max(0, _int(row, "successes")),
        "tr_fault": max(0, _int(row, "tr_fault")),
        "excluded": max(0, _int(row, "excluded")),
        "aborted": max(0, _int(row, "aborted")),
        "distinct_tenants": max(0, _int(row, "distinct_tenants")),
        "coverage": _optional_float(row.get("coverage")),
        "p50_total_ms": _optional_int(row.get("p50_total_ms")),
        "p95_total_ms": _optional_int(row.get("p95_total_ms")),
        "p50_ttft_ms": _optional_int(row.get("p50_ttft_ms")),
        "availability_percent": (
            _optional_float(row.get("availability_percent")) if published else None
        ),
    }


def _status_host_breakdown(value: Any) -> dict[str, dict[str, Any]]:
    rows = value if isinstance(value, Mapping) else {}
    result: dict[str, dict[str, Any]] = {}
    for host in HOSTS:
        row = rows.get(host)
        if not isinstance(row, Mapping):
            continue
        attempts = max(0, _int(row, "attempts"))
        attempt_tr_fault = max(0, _int(row, "attempt_tr_fault"))
        rate = _optional_float(row.get("rate"))
        result[host] = {
            "attempts": attempts,
            "attempt_tr_fault": attempt_tr_fault,
            "rate": (
                rate
                if rate is not None
                else (round(attempt_tr_fault / attempts, 6) if attempts else None)
            ),
        }
    return result


def _status_sdk_breakdown(value: Any) -> dict[str, int]:
    rows = value if isinstance(value, Mapping) else {}
    return {sdk: max(0, _int(rows, sdk)) for sdk in SDKS if sdk in rows}


def _status_canary(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, Mapping) else {}
    return {
        "last_seen_age_seconds": _optional_float(row.get("last_seen_age_seconds")),
        "last_24h_count": max(0, _int(row, "last_24h_count")),
    }


def _status_all_traffic(value: Any) -> dict[str, Any] | None:
    """Project the calibration view; ``None`` when the snapshot predates it."""

    if not isinstance(value, Mapping):
        return None
    raw_windows = value.get("windows")
    windows = raw_windows if isinstance(raw_windows, Mapping) else {}
    return {
        "includes_synthetic": True,
        "gated": False,
        "note": ALL_TRAFFIC_NOTE,
        # Ungated by construction and labelled as such, so the percentage
        # passes through regardless of the publication state.
        "windows": {
            name: _status_window(windows.get(name), published=True) for name in WINDOW_NAMES
        },
        "by_host_24h": _status_host_breakdown(value.get("by_host_24h")),
        "by_sdk_24h": _status_sdk_breakdown(value.get("by_sdk_24h")),
    }


def client_observed_status_section(
    snapshot: Mapping[str, Any] | None,
    *,
    now: dt.datetime,
) -> dict[str, Any]:
    """Project the public snapshot onto the privacy-safe status contract."""

    if not isinstance(snapshot, Mapping):
        return {"available": False, "reason": "no_data"}
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    else:
        now = now.astimezone(dt.UTC)
    age_seconds = _snapshot_age_seconds(snapshot, now=now)
    if age_seconds is None:
        return {"available": False, "reason": "no_data"}
    if age_seconds > 900:
        return {"available": False, "reason": "stale"}

    published = snapshot.get("published") is True
    raw_windows = snapshot.get("windows")
    windows = raw_windows if isinstance(raw_windows, Mapping) else {}
    generated_at = snapshot.get("generated_at")
    return {
        "available": True,
        "state": "published" if published else "calibrating",
        "slo_id": "client_observed",
        "methodology_version": _optional_int(snapshot.get("methodology_version")),
        "windows": {
            name: _status_window(windows.get(name), published=published)
            for name in WINDOW_NAMES
        },
        "by_host_24h": _status_host_breakdown(snapshot.get("by_host_24h")),
        "canary": _status_canary(snapshot.get("canary")),
        # Tolerates a snapshot built by a worker that predates the calibration
        # view: the control plane deploys before the ClickHouse node does.
        "all_traffic": _status_all_traffic(snapshot.get("all_traffic")),
        "generated_at": generated_at if isinstance(generated_at, str) else None,
    }


def _all_traffic_section(
    rows_by_window: Mapping[str, Iterable[Mapping[str, Any]]],
) -> dict[str, Any]:
    """Ungated, uncapped summary of fleet_all rollups, synthetic included.

    A calibration aid while real SDK traffic is scarce: the pipeline can be
    read end to end without touching the published methodology, which stays
    gated, capped, and synthetic-free in ``windows``.
    """

    windows = {
        name: [dict(row) for row in rows_by_window.get(name, ())] for name in WINDOW_NAMES
    }
    return {
        "includes_synthetic": True,
        "gated": False,
        "windows": {name: _window(rows, gated=False) for name, rows in windows.items()},
        "by_host_24h": _host_breakdown(windows["24h"]),
        "by_sdk_24h": _sdk_breakdown(windows["24h"]),
    }


def build_client_reliability(
    rows_by_window: Mapping[str, Iterable[Mapping[str, Any]]],
    now: dt.datetime,
    *,
    signals: Mapping[str, Any] | None = None,
    all_traffic_rows_by_window: Mapping[str, Iterable[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Build the content-free public snapshot from bounded fleet rollups.

    ``all_traffic_rows_by_window`` carries the ``fleet_all`` rollups; when it
    is given the snapshot gains an ``all_traffic`` calibration section and
    nothing else changes.
    """

    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.UTC)
    else:
        now = now.astimezone(dt.UTC)
    windows = {
        name: [dict(row) for row in rows_by_window.get(name, ())]
        for name in WINDOW_NAMES
    }
    watch_15m_rows = [
        dict(row)
        for row in rows_by_window.get("watch_15m", rows_by_window.get("1h", ()))
    ]
    signal_row = signals or {}
    canary_last_received_at = _parse_timestamp(signal_row.get("canary_last_received_at"))
    newest_received_at = _parse_timestamp(signal_row.get("newest_received_at"))
    snapshot: dict[str, Any] = {
        "generated_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "methodology_version": METHODOLOGY_VERSION,
        "published": False,
        "windows": {name: _window(rows) for name, rows in windows.items()},
        "by_host_24h": _host_breakdown(windows["24h"]),
        "by_sdk_24h": _sdk_breakdown(windows["24h"]),
        "canary": {
            "last_seen_age_seconds": _age_seconds(now, canary_last_received_at),
            "last_24h_count": _int(signal_row, "canary_last_24h"),
        },
        "freshness": {
            "newest_received_at": (
                newest_received_at.isoformat().replace("+00:00", "Z")
                if newest_received_at is not None
                else None
            ),
            "drain_lag_seconds": None,
            "age_seconds": _age_seconds(now, newest_received_at),
        },
        "watch": {
            "by_host_15m": _watch_15m(watch_15m_rows),
            "by_host_7d": _watch_7d(windows["7d"]),
        },
    }
    if all_traffic_rows_by_window is not None:
        snapshot["all_traffic"] = _all_traffic_section(all_traffic_rows_by_window)
    return snapshot
