"""Build the weekly client-observed availability calibration report."""

# ruff: noqa: S608
# Every SQL timestamp is rendered from a typed datetime and table names are
# module constants, so no request-controlled fragment reaches these queries.

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import math
import os
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from trusted_router.client_events_schema import TR_CLIENT_SDKS
from trusted_router.client_reliability import ERROR_CLASSES, HOSTS, classify_tr_fault
from trusted_router.storage_models import SyntheticRollup
from trusted_router.synthetic.components import rollup_slo_class_ids

COUNTER_TABLE = "client_minute_counters"
EVENT_TABLE = "client_request_events"
ACTIVITY_TABLE = "activity_generations"
SYNTHETIC_ROLLUP_TABLE = "synthetic_status_rollups"
ROUTER_CORE_SLO_ID = "router_core"
CALIBRATION_DAYS = 14
MAX_WORST_BUCKETS = 20
MAX_WORST_HOURS = 20
AVAILABILITY_TOLERANCE_PERCENTAGE_POINTS = 0.05
COUNTER_ACTIVITY_TOLERANCE = 0.01
MIN_PUBLICATION_REQUESTS = 1_000
MIN_PUBLICATION_TENANTS = 3

log = logging.getLogger("trusted_router.client_availability_calibration")


class ClickHouseExecutor:
    """Run fixed calibration queries against the local ClickHouse node."""

    def __init__(self, *, password: str, database: str = "tr") -> None:
        self._password = password
        self._database = database

    def query(self, sql: str) -> bytes:
        result = subprocess.run(  # noqa: S603 - fixed executable and argv.
            [
                "/usr/bin/clickhouse-client",
                "--user",
                "tr",
                "--password",
                self._password,
                "--database",
                self._database,
                "--query",
                sql,
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace")[:1000]
            raise RuntimeError(f"ClickHouse calibration query failed: {detail}")
        return result.stdout


@dataclasses.dataclass
class _Totals:
    requests: int = 0
    successes: int = 0
    tr_fault: int = 0

    @property
    def denominator(self) -> int:
        return self.successes + self.tr_fault

    @property
    def availability(self) -> float | None:
        if not self.denominator:
            return None
        return self.successes / self.denominator

    @property
    def tr_fault_rate(self) -> float:
        if not self.requests:
            return 0.0
        return self.tr_fault / self.requests


def _utc(value: dt.datetime) -> dt.datetime:
    if not isinstance(value, dt.datetime):
        raise TypeError("ClickHouse query boundaries must be datetimes")
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _ch_time(value: dt.datetime) -> str:
    return _utc(value).strftime("%Y-%m-%d %H:%M:%S")


def _window(start: dt.datetime, end: dt.datetime) -> tuple[str, str]:
    start_text = _ch_time(start)
    end_text = _ch_time(end)
    if _utc(start) >= _utc(end):
        raise ValueError("calibration window start must precede its end")
    return start_text, end_text


def counter_query(*, start: dt.datetime, end: dt.datetime) -> str:
    start_text, end_text = _window(start, end)
    return f"""
SELECT
  toStartOfFiveMinutes(bucket_start) AS bucket,
  tenant_id,
  sdk,
  sdk_version,
  host,
  outcome,
  error_class,
  http_status_class,
  timeout_phase,
  timeout_floor_met,
  provider_pinned,
  sum(requests) AS requests
FROM {COUNTER_TABLE} FINAL
WHERE bucket_start >= toDateTime('{start_text}', 'UTC')
  AND bucket_start < toDateTime('{end_text}', 'UTC')
  AND level = 'request'
  AND synthetic = 0
GROUP BY
  bucket, tenant_id, sdk, sdk_version, host, outcome, error_class,
  http_status_class, timeout_phase, timeout_floor_met, provider_pinned
ORDER BY bucket, tenant_id, sdk, sdk_version, host
FORMAT JSONEachRow
""".strip()


def synthetic_query(*, start: dt.datetime, end: dt.datetime) -> str:
    start_text, end_text = _window(start, end)
    return f"""
SELECT * EXCEPT (ingest_version, latency_histogram, ttfb_histogram,
  dns_histogram, tcp_connect_histogram, tls_handshake_histogram,
  gateway_processing_histogram, error_counts)
FROM {SYNTHETIC_ROLLUP_TABLE} FINAL
WHERE period = '5m'
  AND period_start >= toDateTime('{start_text}', 'UTC')
  AND period_start < toDateTime('{end_text}', 'UTC')
ORDER BY period_start, id
FORMAT JSONEachRow
""".strip()


def attempt_join_query(*, start: dt.datetime, end: dt.datetime) -> str:
    start_text, end_text = _window(start, end)
    activity_start = _ch_time(_utc(start) - dt.timedelta(days=1))
    activity_end = _ch_time(_utc(end) + dt.timedelta(days=1))
    return f"""
SELECT
  event_id,
  final_outcome,
  timeout_phase,
  attempt_host,
  attempt_outcome,
  attempt_error_class,
  attempt_elapsed_ms,
  attempt_request_id,
  activity.gateway_request_id AS server_request_id,
  activity.status AS server_status,
  activity.elapsed_milliseconds AS server_elapsed_ms
FROM
(
  SELECT
    event_id,
    final_outcome,
    timeout_phase,
    attempt_host_value AS attempt_host,
    attempt_outcome_value AS attempt_outcome,
    attempt_error_class_value AS attempt_error_class,
    attempt_elapsed_ms_value AS attempt_elapsed_ms,
    attempt_request_id_value AS attempt_request_id
  FROM {EVENT_TABLE} FINAL
  ARRAY JOIN
    attempt_host AS attempt_host_value,
    attempt_outcome AS attempt_outcome_value,
    attempt_error_class AS attempt_error_class_value,
    attempt_elapsed_ms AS attempt_elapsed_ms_value,
    attempt_request_id AS attempt_request_id_value
  WHERE created_at >= toDateTime64('{start_text}', 3, 'UTC')
    AND created_at < toDateTime64('{end_text}', 3, 'UTC')
    AND synthetic = 0
) AS client
LEFT JOIN
(
  SELECT gateway_request_id, status, elapsed_milliseconds
  FROM {ACTIVITY_TABLE} FINAL
  WHERE created_at >= toDateTime64('{activity_start}', 3, 'UTC')
    AND created_at < toDateTime64('{activity_end}', 3, 'UTC')
    AND gateway_request_id != ''
) AS activity
ON client.attempt_request_id = activity.gateway_request_id
ORDER BY event_id, attempt_request_id
FORMAT JSONEachRow
""".strip()


def activity_success_query(*, start: dt.datetime, end: dt.datetime) -> str:
    start_text, end_text = _window(start, end)
    return f"""
SELECT
  toStartOfHour(created_at) AS bucket,
  tenant_id,
  countIf(status = 'success') AS successes
FROM {ACTIVITY_TABLE} FINAL
WHERE created_at >= toDateTime64('{start_text}', 3, 'UTC')
  AND created_at < toDateTime64('{end_text}', 3, 'UTC')
  AND synthetic = 0
  AND client_source = 'tr'
GROUP BY bucket, tenant_id
ORDER BY bucket, tenant_id
FORMAT JSONEachRow
""".strip()


def canary_query(*, start: dt.datetime, end: dt.datetime) -> str:
    start_text, end_text = _window(start, end)
    return f"""
SELECT toStartOfDay(created_at) AS day, count() AS canary_count
FROM {EVENT_TABLE} FINAL
WHERE created_at >= toDateTime64('{start_text}', 3, 'UTC')
  AND created_at < toDateTime64('{end_text}', 3, 'UTC')
  AND synthetic = 1
GROUP BY day
ORDER BY day
FORMAT JSONEachRow
""".strip()


def build_queries(*, start: dt.datetime, end: dt.datetime) -> dict[str, str]:
    """Render every report query from typed UTC boundaries only."""

    return {
        "counters": counter_query(start=start, end=end),
        "synthetic": synthetic_query(start=start, end=end),
        "attempts": attempt_join_query(start=start, end=end),
        "activity": activity_success_query(start=start, end=end),
        "canary": canary_query(start=start, end=end),
    }


def _parse_rows(payload: bytes) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for line in payload.decode().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict):
            result.append(row)
    return result


def fetch_rows(
    executor: ClickHouseExecutor,
    *,
    start: dt.datetime,
    end: dt.datetime,
) -> dict[str, list[dict[str, Any]]]:
    return {
        name: _parse_rows(executor.query(sql))
        for name, sql in build_queries(start=start, end=end).items()
    }


def _int(row: Mapping[str, Any], key: str) -> int:
    value = row.get(key)
    if value is None or isinstance(value, bool):
        return int(value or 0)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _parse_time(value: Any) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    return _utc(parsed)


def _row_time(row: Mapping[str, Any], *keys: str) -> dt.datetime:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return _parse_time(value)
    raise ValueError(f"row is missing a timestamp in {keys}")


def _five_minute(value: dt.datetime) -> dt.datetime:
    value = _utc(value).replace(second=0, microsecond=0)
    return value.replace(minute=value.minute - value.minute % 5)


def _hour(value: dt.datetime) -> dt.datetime:
    return _utc(value).replace(minute=0, second=0, microsecond=0)


def _time_text(value: dt.datetime) -> str:
    return _utc(value).isoformat().replace("+00:00", "Z")


def _counter_is_tr_fault(row: Mapping[str, Any]) -> bool:
    return classify_tr_fault(
        level="request",
        outcome=str(row.get("outcome") or ""),
        error_class=str(row.get("error_class") or "") or None,
        error_source=None,
        http_status_class_or_status=row.get("http_status_class"),
        host=str(row.get("host") or ""),
        provider_pinned=bool(row.get("provider_pinned")),
        timeout_phase=str(row.get("timeout_phase") or "none"),
        timeout_floor_met=bool(row.get("timeout_floor_met")),
    )


def _safe_closed(value: Any, allowed: tuple[str, ...], fallback: str) -> str:
    text = str(value or "")
    return text if text in allowed else fallback


def _add_counter(total: _Totals, row: Mapping[str, Any]) -> None:
    count = max(0, _int(row, "requests"))
    total.requests += count
    if row.get("outcome") == "ok":
        total.successes += count
    elif _counter_is_tr_fault(row):
        total.tr_fault += count


def _synthetic_rollup(row: Mapping[str, Any]) -> SyntheticRollup:
    return SyntheticRollup(
        id=str(row.get("id") or ""),
        period=str(row.get("period") or "5m"),
        period_start=_time_text(_row_time(row, "period_start", "bucket")),
        component=str(row.get("component") or ""),
        target=str(row.get("target") or ""),
        probe_type=str(row.get("probe_type") or ""),
        monitor_region=str(row.get("monitor_region") or ""),
        target_region=str(row.get("target_region") or "") or None,
        sample_count=max(0, _int(row, "sample_count")),
        up_count=max(0, _int(row, "up_count")),
        down_count=max(0, _int(row, "down_count")),
        degraded_count=max(0, _int(row, "degraded_count")),
        routing_degraded_count=max(0, _int(row, "routing_degraded_count")),
        trust_degraded_count=max(0, _int(row, "trust_degraded_count")),
        unknown_count=max(0, _int(row, "unknown_count")),
    )


def _router_core_rollups(
    rows: Iterable[Mapping[str, Any]],
) -> list[SyntheticRollup]:
    result: list[SyntheticRollup] = []
    for row in rows:
        rollup = _synthetic_rollup(row)
        if ROUTER_CORE_SLO_ID in rollup_slo_class_ids(rollup):
            result.append(rollup)
    return result


def build_agreement_matrix(
    counter_rows: Iterable[Mapping[str, Any]],
    synthetic_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compare exact client request counters with router-core five-minute state."""

    clients: dict[dt.datetime, _Totals] = {}
    hosts: dict[dt.datetime, set[str]] = {}
    for row in counter_rows:
        bucket = _five_minute(_row_time(row, "bucket", "bucket_start"))
        total = clients.setdefault(bucket, _Totals())
        _add_counter(total, row)
        if _counter_is_tr_fault(row):
            host = _safe_closed(row.get("host"), HOSTS, "custom")
            if host:
                hosts.setdefault(bucket, set()).add(host)

    servers: dict[dt.datetime, dict[str, int]] = {}
    for rollup in _router_core_rollups(synthetic_rows):
        bucket = _five_minute(_parse_time(rollup.period_start))
        total = servers.setdefault(bucket, {"samples": 0, "down": 0})
        total["samples"] += rollup.sample_count
        total["down"] += rollup.down_count

    matrix = {
        "client_down_server_up": 0,
        "both_up": 0,
        "client_up_server_down": 0,
        "both_down": 0,
    }
    missed: list[dict[str, Any]] = []
    for bucket in sorted(clients.keys() & servers.keys()):
        client = clients[bucket]
        server = servers[bucket]
        if client.requests <= 0 or server["samples"] <= 0:
            continue
        client_down = client.tr_fault > 0
        server_down = server["down"] > 0
        if client_down and not server_down:
            cell = "client_down_server_up"
        elif not client_down and not server_down:
            cell = "both_up"
        elif not client_down and server_down:
            cell = "client_up_server_down"
        else:
            cell = "both_down"
        matrix[cell] += 1
        if cell == "client_down_server_up":
            missed.append(
                {
                    "bucket": _time_text(bucket),
                    "requests": client.requests,
                    "tr_fault": client.tr_fault,
                    "rate": round(client.tr_fault_rate, 6),
                    "hosts": sorted(hosts.get(bucket, set())),
                }
            )

    missed.sort(key=lambda row: (-float(row["rate"]), -int(row["tr_fault"]), row["bucket"]))
    return {
        "client_down_server_up": missed[:MAX_WORST_BUCKETS],
        "matrix": matrix,
        "buckets_compared": sum(matrix.values()),
    }


def _first(row: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in row:
            return row.get(key)
    return None


def _percentile(values: Sequence[int], percentile: int) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile / 100) - 1)
    return ordered[index]


def build_gateway_request_id_join(
    attempt_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Classify sampled client attempts against server generation rows."""

    never_reached = 0
    by_error_class: dict[str, int] = {}
    by_host: dict[str, int] = {}
    matched = 0
    post_settle_stall = 0
    orphan_client_id = 0
    rtt_values: list[int] = []
    for row in attempt_rows:
        request_id = str(_first(row, "attempt_request_id", "request_id") or "")
        attempt_outcome = str(_first(row, "attempt_outcome", "outcome") or "")
        error_class = _safe_closed(
            _first(row, "attempt_error_class", "error_class"), ERROR_CLASSES, "unknown"
        )
        host = _safe_closed(_first(row, "attempt_host", "host"), HOSTS, "custom")
        server_request_id = str(
            _first(row, "server_request_id", "activity_gateway_request_id") or ""
        )
        server_status_value = _first(row, "server_status", "activity_status")
        server_found = (
            bool(row.get("server_found"))
            or bool(server_request_id)
            or (server_status_value not in (None, ""))
        )

        if not request_id:
            if attempt_outcome in {"transport_error", "timeout"}:
                never_reached += 1
                by_error_class[error_class] = by_error_class.get(error_class, 0) + 1
                by_host[host] = by_host.get(host, 0) + 1
            continue
        if not server_found:
            orphan_client_id += 1
            continue

        matched += 1
        final_outcome = str(row.get("final_outcome") or "")
        if (
            server_status_value == "success"
            and final_outcome in {"stream_broken", "timeout"}
            and row.get("timeout_phase") == "idle"
        ):
            post_settle_stall += 1

        attempt_elapsed = _first(row, "attempt_elapsed_ms", "elapsed_ms")
        server_elapsed = _first(row, "server_elapsed_ms", "elapsed_milliseconds")
        if attempt_elapsed is not None and server_elapsed is not None:
            try:
                rtt_values.append(int(attempt_elapsed) - int(server_elapsed))
            except (TypeError, ValueError):
                pass

    return {
        "never_reached": {
            "count": never_reached,
            "by_error_class": dict(sorted(by_error_class.items())),
            "by_host": dict(sorted(by_host.items())),
        },
        "matched": matched,
        "post_settle_stall": post_settle_stall,
        "orphan_client_id": orphan_client_id,
        "rtt_ms": {
            "count": len(rtt_values),
            "p50": _percentile(rtt_values, 50),
            "p90": _percentile(rtt_values, 90),
            "p99": _percentile(rtt_values, 99),
            "negative_fraction": (
                round(sum(value < 0 for value in rtt_values) / len(rtt_values), 6)
                if rtt_values
                else None
            ),
        },
    }


def _safe_tenant(value: Any) -> str:
    text = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", text):
        return text
    return hashlib.sha256(f"calibration:{text}".encode()).hexdigest()


def _safe_sdk(value: Any) -> str:
    text = str(value or "")
    return text if text in TR_CLIENT_SDKS else "other"


def _safe_version(value: Any) -> str:
    text = str(value or "")
    if re.fullmatch(r"[0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4}([-+][0-9A-Za-z.]{0,20})?", text):
        return text
    return "0.0.0"


def _anomaly_row(
    total: _Totals,
    *,
    fleet_requests: int,
    fleet_fault_rate: float,
) -> dict[str, Any]:
    threshold = max(3 * fleet_fault_rate, fleet_fault_rate + 0.02)
    return {
        "requests": total.requests,
        "availability": (round(total.availability, 6) if total.availability is not None else None),
        "share_of_fleet_requests": (
            round(total.requests / fleet_requests, 6) if fleet_requests else None
        ),
        "tr_fault_rate": round(total.tr_fault_rate, 6),
        "anomaly_threshold": round(threshold, 6),
        "anomalous": total.requests >= 100 and total.tr_fault_rate > threshold,
    }


def build_anomaly_lists(
    counter_rows: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Find tenant and SDK cohorts whose fault rate is not fleet-representative."""

    rows = list(counter_rows)
    fleet = _Totals()
    tenants: dict[str, _Totals] = {}
    sdks: dict[tuple[str, str], _Totals] = {}
    for row in rows:
        _add_counter(fleet, row)
        tenant = _safe_tenant(row.get("tenant_id"))
        sdk = _safe_sdk(row.get("sdk"))
        version = _safe_version(row.get("sdk_version"))
        _add_counter(tenants.setdefault(tenant, _Totals()), row)
        _add_counter(sdks.setdefault((sdk, version), _Totals()), row)

    tenant_rows = [
        {
            "tenant_id": tenant,
            **_anomaly_row(
                total,
                fleet_requests=fleet.requests,
                fleet_fault_rate=fleet.tr_fault_rate,
            ),
        }
        for tenant, total in tenants.items()
    ]
    sdk_rows = [
        {
            "sdk": sdk,
            "sdk_version": version,
            **_anomaly_row(
                total,
                fleet_requests=fleet.requests,
                fleet_fault_rate=fleet.tr_fault_rate,
            ),
        }
        for (sdk, version), total in sdks.items()
    ]
    tenant_rows.sort(
        key=lambda row: (
            not bool(row["anomalous"]),
            -float(row["tr_fault_rate"]),
            row["tenant_id"],
        )
    )
    sdk_rows.sort(
        key=lambda row: (
            not bool(row["anomalous"]),
            -float(row["tr_fault_rate"]),
            row["sdk"],
            row["sdk_version"],
        )
    )
    return {
        "fleet": {
            "requests": fleet.requests,
            "successes": fleet.successes,
            "tr_fault": fleet.tr_fault,
            "availability": (
                round(fleet.availability, 6) if fleet.availability is not None else None
            ),
            "tr_fault_rate": round(fleet.tr_fault_rate, 6),
        },
        "tenants": tenant_rows,
        "sdk_versions": sdk_rows,
    }


def _counter_totals_by_day(
    counter_rows: Iterable[Mapping[str, Any]],
) -> tuple[dict[dt.date, _Totals], dict[dt.date, set[str]]]:
    totals: dict[dt.date, _Totals] = {}
    tenants: dict[dt.date, set[str]] = {}
    for row in counter_rows:
        day = _row_time(row, "bucket", "bucket_start").date()
        _add_counter(totals.setdefault(day, _Totals()), row)
        if _int(row, "requests") > 0:
            tenants.setdefault(day, set()).add(_safe_tenant(row.get("tenant_id")))
    return totals, tenants


def _counter_successes_by_tenant_hour(
    counter_rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[dt.date, dt.datetime, str], int]:
    result: dict[tuple[dt.date, dt.datetime, str], int] = {}
    for row in counter_rows:
        if row.get("outcome") != "ok":
            continue
        hour = _hour(_row_time(row, "bucket", "bucket_start"))
        tenant = _safe_tenant(row.get("tenant_id"))
        key = (hour.date(), hour, tenant)
        result[key] = result.get(key, 0) + max(0, _int(row, "requests"))
    return result


def _activity_successes_by_tenant_hour(
    activity_rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[dt.date, dt.datetime, str], int]:
    result: dict[tuple[dt.date, dt.datetime, str], int] = {}
    for row in activity_rows:
        hour = _hour(_row_time(row, "bucket", "bucket_start", "hour"))
        tenant = _safe_tenant(row.get("tenant_id"))
        key = (hour.date(), hour, tenant)
        result[key] = result.get(key, 0) + max(0, _int(row, "successes"))
    return result


def _counter_activity_agreement(
    counter_rows: Sequence[Mapping[str, Any]],
    activity_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[dt.date, dict[str, Any]], list[dict[str, Any]]]:
    client = _counter_successes_by_tenant_hour(counter_rows)
    server = _activity_successes_by_tenant_hour(activity_rows)
    daily: dict[dt.date, dict[str, Any]] = {}
    offenders: list[dict[str, Any]] = []
    for day, hour, tenant in sorted(client.keys() | server.keys()):
        client_successes = client.get((day, hour, tenant), 0)
        server_successes = server.get((day, hour, tenant), 0)
        difference = abs(client_successes - server_successes) / max(server_successes, 1)
        item = daily.setdefault(
            day,
            {"hours_compared": 0, "outside_tolerance": 0, "max_difference": 0.0},
        )
        item["hours_compared"] += 1
        item["max_difference"] = max(float(item["max_difference"]), difference)
        if difference > COUNTER_ACTIVITY_TOLERANCE:
            item["outside_tolerance"] += 1
        if difference > COUNTER_ACTIVITY_TOLERANCE:
            offenders.append(
                {
                    "hour": _time_text(hour),
                    "tenant_id": tenant,
                    "client_successes": client_successes,
                    "server_successes": server_successes,
                    "difference_fraction": round(difference, 6),
                }
            )
    offenders.sort(
        key=lambda row: (
            -float(row["difference_fraction"]),
            row["hour"],
            row["tenant_id"],
        )
    )
    return daily, offenders[:MAX_WORST_HOURS]


def _synthetic_by_day(
    synthetic_rows: Iterable[Mapping[str, Any]],
) -> dict[dt.date, dict[str, int]]:
    result: dict[dt.date, dict[str, int]] = {}
    for rollup in _router_core_rollups(synthetic_rows):
        day = _parse_time(rollup.period_start).date()
        item = result.setdefault(day, {"samples": 0, "up": 0, "down": 0})
        item["samples"] += rollup.sample_count
        item["up"] += rollup.up_count
        item["down"] += rollup.down_count
    return result


def _canary_by_day(
    canary_rows: Iterable[Mapping[str, Any]],
) -> dict[dt.date, int]:
    result: dict[dt.date, int] = {}
    for row in canary_rows:
        day = _row_time(row, "day", "created_at", "bucket").date()
        result[day] = result.get(day, 0) + max(
            0,
            _int(row, "canary_count") or _int(row, "count"),
        )
    return result


def build_publication_verdict(
    counter_rows: Iterable[Mapping[str, Any]],
    activity_rows: Iterable[Mapping[str, Any]],
    synthetic_rows: Iterable[Mapping[str, Any]],
    canary_rows: Iterable[Mapping[str, Any]],
    *,
    start: dt.datetime,
    end: dt.datetime,
    min_canary_per_day: int = 200,
) -> dict[str, Any]:
    """Evaluate every calibration gate for each closed UTC day in the window."""

    start = _utc(start)
    end = _utc(end)
    if start >= end:
        raise ValueError("calibration window start must precede its end")
    if min_canary_per_day < 1:
        raise ValueError("min_canary_per_day must be positive")
    counters = list(counter_rows)
    activities = list(activity_rows)
    synthetics = list(synthetic_rows)
    canaries = list(canary_rows)
    client_daily, client_tenants = _counter_totals_by_day(counters)
    agreement_daily, worst_offenders = _counter_activity_agreement(counters, activities)
    synthetic_daily = _synthetic_by_day(synthetics)
    canary_daily = _canary_by_day(canaries)

    days: list[dict[str, Any]] = []
    day = start.date()
    while day < end.date():
        client = client_daily.get(day, _Totals())
        agreement = agreement_daily.get(
            day,
            {"hours_compared": 0, "outside_tolerance": 0, "max_difference": 0.0},
        )
        synthetic = synthetic_daily.get(day, {"samples": 0, "up": 0, "down": 0})
        synthetic_availability = (
            synthetic["up"] / synthetic["samples"] * 100 if synthetic["samples"] else None
        )
        client_availability = client.availability * 100 if client.availability is not None else None
        incident_free = synthetic["samples"] > 0 and synthetic["down"] == 0
        counter_activity_gate = (
            int(agreement["hours_compared"]) > 0 and int(agreement["outside_tolerance"]) == 0
        )
        availability_gate = bool(
            incident_free
            and client_availability is not None
            and synthetic_availability is not None
            and client_availability
            >= synthetic_availability - AVAILABILITY_TOLERANCE_PERCENTAGE_POINTS
        )
        canary_count = canary_daily.get(day, 0)
        canary_gate = canary_count >= min_canary_per_day
        distinct_tenants = len(client_tenants.get(day, set()))
        threshold_gate = (
            client.requests >= MIN_PUBLICATION_REQUESTS
            and distinct_tenants >= MIN_PUBLICATION_TENANTS
        )
        gates = {
            "counter_activity_agreement": counter_activity_gate,
            "availability_vs_synthetic": availability_gate,
            "negative_controls": canary_gate,
            "publication_thresholds": threshold_gate,
        }
        days.append(
            {
                "day": day.isoformat(),
                "clean": all(gates.values()),
                "gates": gates,
                "measurements": {
                    "counter_activity_hours": int(agreement["hours_compared"]),
                    "counter_activity_outside_tolerance": int(agreement["outside_tolerance"]),
                    "counter_activity_max_difference_fraction": round(
                        float(agreement["max_difference"]), 6
                    ),
                    "incident_free": incident_free,
                    "client_availability_percent": (
                        round(client_availability, 6) if client_availability is not None else None
                    ),
                    "synthetic_availability_percent": (
                        round(synthetic_availability, 6)
                        if synthetic_availability is not None
                        else None
                    ),
                    "canary_count": canary_count,
                    "min_canary_per_day": min_canary_per_day,
                    "requests": client.requests,
                    "distinct_tenants": distinct_tenants,
                },
            }
        )
        day += dt.timedelta(days=1)

    clean_days = 0
    for item in reversed(days):
        if not item["clean"]:
            break
        clean_days += 1
    calibration_window = days[-CALIBRATION_DAYS:]
    gate_names = (
        "counter_activity_agreement",
        "availability_vs_synthetic",
        "negative_controls",
        "publication_thresholds",
    )
    gates = {
        name: bool(calibration_window)
        and all(bool(item["gates"][name]) for item in calibration_window)
        for name in gate_names
    }
    blockers = [name for name in gate_names if not gates[name]]
    if clean_days < CALIBRATION_DAYS:
        blockers.append("insufficient_consecutive_clean_days")
    return {
        "clean_days": clean_days,
        "days": days,
        "ready_to_publish": clean_days >= CALIBRATION_DAYS,
        "blockers": blockers,
        "gates": gates,
        "worst_offender_hours": worst_offenders,
    }


def build_calibration_report(
    *,
    counter_rows: Iterable[Mapping[str, Any]],
    synthetic_rows: Iterable[Mapping[str, Any]],
    attempt_rows: Iterable[Mapping[str, Any]],
    activity_rows: Iterable[Mapping[str, Any]],
    canary_rows: Iterable[Mapping[str, Any]],
    start: dt.datetime,
    end: dt.datetime,
    min_canary_per_day: int = 200,
) -> dict[str, Any]:
    """Compose the complete content-free report from already-fetched rows."""

    counters = list(counter_rows)
    synthetics = list(synthetic_rows)
    return {
        "agreement": build_agreement_matrix(counters, synthetics),
        "gateway_request_id_join": build_gateway_request_id_join(attempt_rows),
        "anomalies": build_anomaly_lists(counters),
        "publication": build_publication_verdict(
            counters,
            activity_rows,
            synthetics,
            canary_rows,
            start=start,
            end=end,
            min_canary_per_day=min_canary_per_day,
        ),
        "window": {"start": _time_text(start), "end": _time_text(end)},
    }


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _parse_at(value: str) -> dt.datetime:
    try:
        return _utc(dt.datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO timestamp") from exc


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=_positive_int, default=7)
    parser.add_argument("--at", type=_parse_at, help="UTC ISO timestamp; defaults to now")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--min-canary-per-day", type=_positive_int, default=200)
    return parser.parse_args(argv)


def _closed_window(*, at: dt.datetime, days: int) -> tuple[dt.datetime, dt.datetime]:
    end = _utc(at).replace(hour=0, minute=0, second=0, microsecond=0)
    return end - dt.timedelta(days=days), end


def _human_summary(report: Mapping[str, Any]) -> str:
    agreement = report["agreement"]
    publication = report["publication"]
    return (
        "client_down_server_up="
        f"{agreement['matrix']['client_down_server_up']} "
        f"buckets_compared={agreement['buckets_compared']} "
        f"clean_days={publication['clean_days']} "
        f"ready_to_publish={str(publication['ready_to_publish']).lower()} "
        f"blockers={','.join(publication['blockers']) or 'none'}"
    )


def main(
    argv: list[str] | None = None,
    *,
    executor: ClickHouseExecutor | None = None,
    now: dt.datetime | None = None,
    out: Callable[[str], None] = print,
) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    at = args.at or now or dt.datetime.now(dt.UTC)
    start, end = _closed_window(at=at, days=args.days)
    if executor is None:
        password = os.environ.get("CH_PASSWORD", "")
        if not password:
            raise SystemExit("CH_PASSWORD is required")
        executor = ClickHouseExecutor(password=password)
    rows = fetch_rows(executor, start=start, end=end)
    report = build_calibration_report(
        counter_rows=rows["counters"],
        synthetic_rows=rows["synthetic"],
        attempt_rows=rows["attempts"],
        activity_rows=rows["activity"],
        canary_rows=rows["canary"],
        start=start,
        end=end,
        min_canary_per_day=args.min_canary_per_day,
    )
    if args.json_out is not None:
        args.json_out.write_text(
            json.dumps(report, indent=2) + "\n",
            encoding="utf-8",
        )
    out(_human_summary(report))
    publication = report["publication"]
    log.info(
        "client_availability_calibration.metrics days=%d clean_days=%d ready_to_publish=%d "
        "client_down_server_up=%d dry_run=%d",
        args.days,
        publication["clean_days"],
        int(publication["ready_to_publish"]),
        report["agreement"]["matrix"]["client_down_server_up"],
        int(args.dry_run),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
