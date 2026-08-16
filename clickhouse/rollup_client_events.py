"""Recompute client-observed availability rollups from exact minute counters."""

# ruff: noqa: S608
# Every SQL timestamp is rendered from a typed datetime and table names are
# module constants, so no request-controlled fragment reaches these queries.

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import subprocess
from collections.abc import Iterable, Mapping
from typing import Any

from trusted_router.client_reliability import (
    METHODOLOGY_VERSION,
    classify_tr_fault,
    is_excluded,
)

COUNTER_TABLE = "client_minute_counters"
ACTIVITY_TABLE = "activity_generations"
ROLLUP_TABLE = "client_availability_rollups"

log = logging.getLogger("trusted_router.client_reliability_rollup")


class ClickHouseExecutor:
    def __init__(self, *, password: str, database: str = "tr") -> None:
        self._password = password
        self._database = database

    def query(self, sql: str, *, input_bytes: bytes | None = None) -> bytes:
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
            input=input_bytes,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode(errors="replace")[:1000])
        return result.stdout


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _ch_time(value: dt.datetime) -> str:
    return _utc(value).strftime("%Y-%m-%d %H:%M:%S")


def _parse_time(value: Any) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(str(value).replace(" ", "T").replace("Z", "+00:00"))
    return _utc(parsed)


def _floor(value: dt.datetime, period: str) -> dt.datetime:
    value = _utc(value).replace(second=0, microsecond=0)
    if period == "5m":
        return value.replace(minute=value.minute - value.minute % 5)
    if period == "hour":
        return value.replace(minute=0)
    if period == "day":
        return value.replace(hour=0, minute=0)
    raise ValueError(f"unsupported client rollup period: {period}")


def _period_delta(period: str) -> dt.timedelta:
    if period == "5m":
        return dt.timedelta(minutes=5)
    if period == "hour":
        return dt.timedelta(hours=1)
    if period == "day":
        return dt.timedelta(days=1)
    raise ValueError(f"unsupported client rollup period: {period}")


def client_rollup_id(
    *,
    period: str,
    period_start: dt.datetime,
    scope: str,
    tenant_id: str,
    host: str,
    endpoint: str,
    sdk: str,
) -> str:
    key = (
        period,
        _utc(period_start).isoformat(),
        scope,
        tenant_id,
        host,
        endpoint,
        sdk,
    )
    return hashlib.blake2b("\x1f".join(key).encode(), digest_size=16).hexdigest()


def cap_tenant_requests(
    requests_by_tenant: Mapping[str, int],
) -> tuple[dict[str, int], int]:
    """Cap each tenant at 25% of the uncapped fleet request count."""

    normalized = {tenant: max(0, int(count)) for tenant, count in requests_by_tenant.items()}
    total = sum(normalized.values())
    if not total:
        return normalized, 0
    limit = max(1, total // 4)
    allowed = {tenant: min(count, limit) for tenant, count in normalized.items()}
    return allowed, total - sum(allowed.values())


def _new_accumulator() -> dict[str, Any]:
    return {
        "requests": 0,
        "successes": 0,
        "tr_fault_failures": 0,
        "excluded_failures": 0,
        "aborted": 0,
        "attempts": 0,
        "attempt_tr_fault": 0,
        "failover_used": 0,
        "first_attempt_success": 0,
        "total_ms_hist": {},
        "first_event_ms_hist": {},
    }


def _merge_histogram(target: dict[str, int], source: Any) -> None:
    if not isinstance(source, Mapping):
        return
    for bucket, count in source.items():
        target[str(bucket)] = target.get(str(bucket), 0) + int(count)


def _classification_args(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "level": str(row.get("level") or "request"),
        "outcome": str(row.get("outcome") or ""),
        "error_class": str(row.get("error_class") or "") or None,
        "error_source": str(row.get("error_source") or "") or None,
        "http_status_class_or_status": row.get("http_status_class"),
        "host": str(row.get("host") or ""),
        "provider_pinned": bool(row.get("provider_pinned")),
        "timeout_phase": str(row.get("timeout_phase") or "none"),
        "timeout_floor_met": bool(row.get("timeout_floor_met")),
    }


def _is_tr_fault(row: Mapping[str, Any]) -> bool:
    if "tr_fault" in row:
        return bool(row.get("tr_fault"))
    return classify_tr_fault(**_classification_args(row))


def _facets(row: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    values = [
        ("", "", ""),
        (str(row.get("host") or ""), "", ""),
        ("", str(row.get("endpoint") or ""), ""),
        ("", "", str(row.get("sdk") or "")),
    ]
    return tuple(dict.fromkeys(value for value in values if any(value) or value == values[0]))


def _apply_counter(target: dict[str, Any], row: Mapping[str, Any]) -> None:
    count = int(row.get("requests") or 0)
    outcome = str(row.get("outcome") or "")
    if row.get("level") == "request":
        target["requests"] += count
        if outcome == "ok":
            target["successes"] += count
        elif _is_tr_fault(row):
            target["tr_fault_failures"] += count
        else:
            # Calling the pure exclusion policy here keeps the disclosed
            # denominator policy centralized. A future non-fault category is
            # conservatively disclosed as excluded too.
            is_excluded(**_classification_args(row))
            target["excluded_failures"] += count
        if outcome == "aborted":
            target["aborted"] += count
        target["first_attempt_success"] += int(row.get("first_attempt_success") or 0)
        _merge_histogram(target["total_ms_hist"], row.get("total_ms_hist"))
        _merge_histogram(target["first_event_ms_hist"], row.get("first_event_ms_hist"))
    elif row.get("level") == "attempt":
        attempts = int(row.get("attempts") or 0)
        target["attempts"] += attempts
        if _is_tr_fault(row):
            target["attempt_tr_fault"] += attempts
        target["failover_used"] += int(row.get("failover_used") or 0)


def _tenant_accumulators(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, dict[tuple[str, str, str], dict[str, Any]]]:
    grouped: dict[str, dict[tuple[str, str, str], dict[str, Any]]] = {}
    for row in rows:
        tenant_id = str(row.get("tenant_id") or "")
        tenant = grouped.setdefault(tenant_id, {})
        for facet in _facets(row):
            target = tenant.setdefault(facet, _new_accumulator())
            _apply_counter(target, row)
    return grouped


def _scaled(value: int, allowed: int, requests: int) -> int:
    return value if requests <= 0 else value * allowed // requests


def _scaled_accumulator(
    source: Mapping[str, Any],
    *,
    allowed: int,
    requests: int,
) -> dict[str, Any]:
    result = _new_accumulator()
    for field in (
        "requests",
        "successes",
        "tr_fault_failures",
        "excluded_failures",
        "aborted",
        "attempts",
        "attempt_tr_fault",
        "failover_used",
        "first_attempt_success",
    ):
        result[field] = _scaled(int(source.get(field) or 0), allowed, requests)
    for field in ("total_ms_hist", "first_event_ms_hist"):
        histogram = source.get(field) or {}
        if isinstance(histogram, Mapping):
            result[field] = {
                str(bucket): _scaled(int(count), allowed, requests)
                for bucket, count in histogram.items()
            }
    return result


def _merge_accumulator(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for field in (
        "requests",
        "successes",
        "tr_fault_failures",
        "excluded_failures",
        "aborted",
        "attempts",
        "attempt_tr_fault",
        "failover_used",
        "first_attempt_success",
    ):
        target[field] += int(source.get(field) or 0)
    for field in ("total_ms_hist", "first_event_ms_hist"):
        _merge_histogram(target[field], source.get(field))


def _coverage_by_tenant(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for row in rows:
        if bool(row.get("synthetic")):
            continue
        tenant = str(row.get("tenant_id") or "")
        result[tenant] = result.get(tenant, 0) + int(row.get("requests") or 0)
    return result


def _render_rollup(
    accumulator: Mapping[str, Any],
    *,
    period: str,
    period_start: dt.datetime,
    scope: str,
    tenant_id: str,
    facet: tuple[str, str, str],
    distinct_tenants: int,
    capped_requests: int,
    coverage_requests: int,
    computed_at: dt.datetime,
) -> dict[str, Any]:
    host, endpoint, sdk = facet
    return {
        "id": client_rollup_id(
            period=period,
            period_start=period_start,
            scope=scope,
            tenant_id=tenant_id,
            host=host,
            endpoint=endpoint,
            sdk=sdk,
        ),
        "period": period,
        "period_start": _utc(period_start).isoformat(),
        "scope": scope,
        "tenant_id": tenant_id,
        "host": host,
        "endpoint": endpoint,
        "sdk": sdk,
        **accumulator,
        "distinct_tenants": distinct_tenants,
        "capped_requests": capped_requests,
        "coverage_requests": coverage_requests,
        "methodology_version": METHODOLOGY_VERSION,
        "computed_at": _utc(computed_at).isoformat(),
    }


def aggregate_client_rollups(
    counter_rows: Iterable[Mapping[str, Any]],
    coverage_rows: Iterable[Mapping[str, Any]],
    *,
    period: str,
    period_start: dt.datetime,
    computed_at: dt.datetime,
) -> list[dict[str, Any]]:
    """Aggregate one period for tenant and capped, non-synthetic fleet scopes."""

    period_start = _floor(period_start, period)
    period_end = period_start + _period_delta(period)
    counters = [
        row for row in counter_rows if period_start <= _parse_time(row["bucket_start"]) < period_end
    ]
    coverage = [
        row
        for row in coverage_rows
        if period_start <= _parse_time(row["bucket_start"]) < period_end
    ]
    tenant_groups = _tenant_accumulators(counters)
    coverage_tenants = _coverage_by_tenant(coverage)
    result: list[dict[str, Any]] = []
    for tenant_id, facets in tenant_groups.items():
        for facet, accumulator in facets.items():
            result.append(
                _render_rollup(
                    accumulator,
                    period=period,
                    period_start=period_start,
                    scope="tenant",
                    tenant_id=tenant_id,
                    facet=facet,
                    distinct_tenants=1,
                    capped_requests=0,
                    coverage_requests=coverage_tenants.get(tenant_id, 0),
                    computed_at=computed_at,
                )
            )

    fleet_groups = _tenant_accumulators(row for row in counters if not bool(row.get("synthetic")))
    requests_by_tenant = {
        tenant: int(facets.get(("", "", ""), {}).get("requests") or 0)
        for tenant, facets in fleet_groups.items()
    }
    allowed, total_capped = cap_tenant_requests(requests_by_tenant)
    fleet_facets = {facet for facets in fleet_groups.values() for facet in facets}
    for facet in sorted(fleet_facets):
        accumulator = _new_accumulator()
        contributors = 0
        facet_capped = 0
        for tenant, facets in fleet_groups.items():
            source = facets.get(facet)
            if source is None:
                continue
            requests = requests_by_tenant[tenant]
            tenant_allowed = allowed[tenant]
            scaled = _scaled_accumulator(
                source,
                allowed=tenant_allowed,
                requests=requests,
            )
            _merge_accumulator(accumulator, scaled)
            if int(source.get("requests") or 0):
                contributors += 1
            facet_capped += max(0, int(source.get("requests") or 0) - int(scaled["requests"]))
        result.append(
            _render_rollup(
                accumulator,
                period=period,
                period_start=period_start,
                scope="fleet",
                tenant_id="",
                facet=facet,
                distinct_tenants=contributors,
                capped_requests=(total_capped if facet == ("", "", "") else facet_capped),
                coverage_requests=sum(coverage_tenants.values()),
                computed_at=computed_at,
            )
        )
    return result


def fetch_inputs(
    executor: ClickHouseExecutor,
    *,
    start: dt.datetime,
    end: dt.datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    counters = executor.query(
        f"SELECT * EXCEPT ingest_version FROM {COUNTER_TABLE} FINAL "
        f"WHERE bucket_start >= toDateTime('{_ch_time(start)}', 'UTC') "
        f"AND bucket_start < toDateTime('{_ch_time(end)}', 'UTC') "
        "AND received_at <= toDateTime64(bucket_start, 3, 'UTC') + INTERVAL 6 HOUR "
        "ORDER BY bucket_start, event_id FORMAT JSONEachRow"
    )
    coverage = executor.query(
        "SELECT toStartOfMinute(created_at) AS bucket_start, tenant_id, "
        "synthetic, count() AS requests "
        f"FROM {ACTIVITY_TABLE} FINAL "
        f"WHERE created_at >= toDateTime64('{_ch_time(start)}', 3, 'UTC') "
        f"AND created_at < toDateTime64('{_ch_time(end)}', 3, 'UTC') "
        "GROUP BY bucket_start, tenant_id, synthetic "
        "ORDER BY bucket_start, tenant_id FORMAT JSONEachRow"
    )
    return (
        [json.loads(line) for line in counters.decode().splitlines() if line.strip()],
        [json.loads(line) for line in coverage.decode().splitlines() if line.strip()],
    )


def _starts(now: dt.datetime, *, period: str, lookback: dt.timedelta) -> list[dt.datetime]:
    end = _floor(now, period)
    current = _floor(now - lookback, period)
    result: list[dt.datetime] = []
    while current <= end:
        result.append(current)
        current += _period_delta(period)
    return result


def recompute(
    executor: ClickHouseExecutor,
    *,
    now: dt.datetime,
    dry_run: bool = False,
) -> dict[str, int]:
    now = _utc(now)
    start = _floor(now - dt.timedelta(days=3), "day")
    counters, coverage = fetch_inputs(executor, start=start, end=now)
    computed_at = dt.datetime.now(dt.UTC)
    rollups: list[dict[str, Any]] = []
    for period, lookback in (
        ("5m", dt.timedelta(hours=3)),
        ("hour", dt.timedelta(hours=3)),
        ("day", dt.timedelta(days=3)),
    ):
        for period_start in _starts(now, period=period, lookback=lookback):
            rollups.extend(
                aggregate_client_rollups(
                    counters,
                    coverage,
                    period=period,
                    period_start=period_start,
                    computed_at=computed_at,
                )
            )
    if rollups and not dry_run:
        payload = b"\n".join(
            json.dumps(row, separators=(",", ":"), sort_keys=True).encode() for row in rollups
        )
        executor.query(
            f"INSERT INTO {ROLLUP_TABLE} FORMAT JSONEachRow",
            input_bytes=payload,
        )
    return {
        "counter_rows": len(counters),
        "coverage_rows": len(coverage),
        "rollups": len(rollups),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--at", help="UTC ISO timestamp; defaults to now")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    now = (
        dt.datetime.fromisoformat(args.at.replace("Z", "+00:00"))
        if args.at
        else dt.datetime.now(dt.UTC)
    )
    result = recompute(
        ClickHouseExecutor(password=password),
        now=now,
        dry_run=args.dry_run,
    )
    log.info(
        "client_reliability_rollup.metrics counter_rows=%d coverage_rows=%d rollups=%d dry_run=%d",
        result["counter_rows"],
        result["coverage_rows"],
        result["rollups"],
        int(args.dry_run),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
