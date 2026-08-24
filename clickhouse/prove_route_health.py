"""Differential proof for the route-health consumer on ClickHouse.

This is a read-only stage-2 proof.  It evaluates the current production
``evaluate_route_health`` function against the production Bigtable-backed
store, evaluates the same routes and wall-clock window in one ClickHouse SQL
query, and compares every field of every flagged route.

Run it through an IAP tunnel to the internal-only ClickHouse HTTP interface::

    CH_PASSWORD=... uv run python clickhouse/prove_route_health.py

``CH_URL`` defaults to ``http://127.0.0.1:18123/`` so the tunnel can forward
that local port to port 8123 on ``tr-clickhouse-1``.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast
from unittest import mock

import httpx
from google.cloud import bigtable

from trusted_router.storage_gcp_benchmark_index import (
    provider_benchmark_samples as bigtable_provider_benchmark_samples,
)
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.store_protocol import Store
from trusted_router.synthetic import route_health
from trusted_router.synthetic.probes import rotation_candidates
from trusted_router.synthetic.route_health import RouteHealthFlag

PROJECT = "quill-cloud-proxy"
BIGTABLE_INSTANCE = "trusted-router-logs"
BIGTABLE_TABLE = "trustedrouter-generations"
BIGTABLE_FAMILIES = ("benchmark", "m")

CH_URL = os.environ.get("CH_URL", "http://127.0.0.1:18123/")
CH_USER = os.environ.get("CH_USER", "tr")

WINDOW_HOURS = 48
MIN_SAMPLES = 6
FAILURE_THRESHOLD = 0.95
SAMPLES_PER_ROUTE_LIMIT = 48


ROUTE_HEALTH_SQL = """
WITH
    coverage AS
    (
        SELECT
            minOrNull(created_at) AS oldest_created_at,
            maxOrNull(created_at) AS newest_created_at,
            count() AS table_rows,
            countIf(created_at >= {cutoff:DateTime64(3, 'UTC')}) AS window_rows
        FROM tr.provider_benchmark_samples FINAL
    ),
    ranked AS
    (
        SELECT
            provider,
            model,
            created_at,
            status,
            source,
            error_status,
            error_type,
            error_message,
            row_number() OVER (
                PARTITION BY provider, model
                ORDER BY created_at DESC
            ) AS route_row_number
        FROM tr.provider_benchmark_samples FINAL
        WHERE has(
            JSONExtract(
                {routes_json:String},
                'Array(Tuple(String, String))'
            ),
            tuple(toString(provider), toString(model))
        )
    ),
    eligible AS
    (
        SELECT
            provider,
            model,
            created_at,
            status,
            error_type,
            error_message
        FROM ranked
        WHERE route_row_number <= {route_limit:UInt32}
          AND source = 'synthetic'
          AND created_at >= {cutoff:DateTime64(3, 'UTC')}
          AND status != 'unsupported'
          AND status IN ('error', 'success')
          AND NOT (
              status = 'error'
              AND (
                  ifNull(error_status, 0) IN (429, 500, 502, 503, 504, 529)
                  OR ifNull(error_type, '') IN (
                      'ReadTimeout',
                      'ConnectTimeout',
                      'WriteTimeout',
                      'PoolTimeout',
                      'ConnectError',
                      'ReadError',
                      'WriteError',
                      'RemoteProtocolError'
                  )
              )
          )
    ),
    flagged AS
    (
        SELECT
            provider,
            model,
            count() AS samples,
            countIf(status = 'error') AS failures,
            failures / samples AS failure_rate,
            argMaxIf(
                tuple(error_type, error_message),
                created_at,
                status = 'error'
            ) AS newest_error
        FROM eligible
        GROUP BY provider, model
        HAVING samples >= {min_samples:UInt32}
           AND failure_rate >= {failure_threshold:Float64}
    )
SELECT
    'coverage' AS kind,
    CAST(NULL, 'Nullable(String)') AS provider,
    CAST(NULL, 'Nullable(String)') AS model,
    CAST(NULL, 'Nullable(UInt64)') AS samples,
    CAST(NULL, 'Nullable(UInt64)') AS failures,
    CAST(NULL, 'Nullable(Float64)') AS failure_rate,
    CAST(NULL, 'Nullable(String)') AS newest_error_type,
    CAST(NULL, 'Nullable(String)') AS newest_error_message,
    oldest_created_at,
    newest_created_at,
    toNullable(table_rows) AS table_rows,
    toNullable(window_rows) AS window_rows
FROM coverage

UNION ALL

SELECT
    'flag' AS kind,
    toNullable(toString(provider)) AS provider,
    toNullable(toString(model)) AS model,
    toNullable(samples) AS samples,
    toNullable(failures) AS failures,
    toNullable(failure_rate) AS failure_rate,
    CAST(tupleElement(newest_error, 1), 'Nullable(String)') AS newest_error_type,
    CAST(tupleElement(newest_error, 2), 'Nullable(String)') AS newest_error_message,
    CAST(NULL, 'Nullable(DateTime64(3, \\'UTC\\'))') AS oldest_created_at,
    CAST(NULL, 'Nullable(DateTime64(3, \\'UTC\\'))') AS newest_created_at,
    CAST(NULL, 'Nullable(UInt64)') AS table_rows,
    CAST(NULL, 'Nullable(UInt64)') AS window_rows
FROM flagged
ORDER BY kind, provider, model
FORMAT JSONEachRow
"""


@dataclass(frozen=True)
class WindowCoverage:
    oldest_created_at: dt.datetime
    newest_created_at: dt.datetime
    table_rows: int
    window_rows: int


class RealBenchmarkStore:
    """The production Bigtable benchmark read path, without unrelated Spanner IO.

    The composite ``SpannerBigtableStore`` eagerly opens a Spanner session
    pool, but route health only calls this Bigtable method.  Using the exact
    production index function and family order keeps this proof scoped to the
    real store data and semantics without requiring Spanner write-service
    permissions.
    """

    def __init__(self, *, project: str, instance: str, table: str) -> None:
        client = bigtable.Client(project=project, admin=False)
        self._table = client.instance(instance).table(table)

    def provider_benchmark_samples(
        self,
        *,
        date: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int = 1000,
    ) -> list[ProviderBenchmarkSample]:
        return bigtable_provider_benchmark_samples(
            self._table,
            BIGTABLE_FAMILIES,
            date=date,
            provider=provider,
            model=model,
            limit=limit,
        )


def _millisecond_now() -> dt.datetime:
    """Return wall clock at the same precision as ClickHouse DateTime64(3)."""
    now = dt.datetime.now(dt.UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _production_routes() -> list[tuple[str, str]]:
    routes = [
        (provider, model)
        for provider, models in rotation_candidates().items()
        for model in models
    ]
    if not routes:
        raise SystemExit("production rotation has no routes; refusing a trivial proof")
    if len(routes) != len(set(routes)):
        raise SystemExit("production rotation contains duplicate provider/model routes")
    return routes


def _real_store(args: argparse.Namespace) -> Store:
    return cast(
        Store,
        RealBenchmarkStore(
            project=args.project,
            instance=args.bigtable_instance,
            table=args.bigtable_table,
        ),
    )


def python_flags(
    store: Store,
    *,
    routes: list[tuple[str, str]],
    evaluated_at: dt.datetime,
    window_hours: int,
    min_samples: int,
    failure_threshold: float,
) -> list[RouteHealthFlag]:
    """Call the production evaluator with a fixed, shared wall clock."""

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> FrozenDateTime:
            value = evaluated_at if tz is None else evaluated_at.astimezone(tz)
            if tz is None:
                value = value.replace(tzinfo=None)
            return cls.fromtimestamp(value.timestamp(), tz=value.tzinfo)

    frozen_datetime_module = SimpleNamespace(
        UTC=dt.UTC,
        datetime=FrozenDateTime,
        timedelta=dt.timedelta,
    )
    with mock.patch.object(route_health, "dt", frozen_datetime_module):
        return route_health.evaluate_route_health(
            store,
            routes=routes,
            window_hours=window_hours,
            min_samples=min_samples,
            failure_threshold=failure_threshold,
        )


def _clickhouse_request(
    query: str,
    *,
    params: dict[str, str],
    password: str,
) -> str:
    """Run SQL with values bound by ClickHouse server-side parameters."""
    query_params = {f"param_{name}": value for name, value in params.items()}
    response = httpx.post(
        CH_URL,
        params=query_params,
        content=query.encode(),
        auth=(CH_USER, password),
        timeout=300,
    )
    response.raise_for_status()
    return response.text


def _parse_clickhouse_datetime(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise ValueError(f"expected ClickHouse datetime string, got {value!r}")
    parsed = dt.datetime.fromisoformat(value.replace(" ", "T"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed.astimezone(dt.UTC)


def clickhouse_flags(
    *,
    routes: list[tuple[str, str]],
    cutoff: dt.datetime,
    route_limit: int,
    min_samples: int,
    failure_threshold: float,
    password: str,
) -> tuple[list[RouteHealthFlag], WindowCoverage]:
    output = _clickhouse_request(
        ROUTE_HEALTH_SQL,
        params={
            "routes_json": json.dumps(routes, separators=(",", ":")),
            "cutoff": cutoff.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "route_limit": str(route_limit),
            "min_samples": str(min_samples),
            "failure_threshold": repr(failure_threshold),
        },
        password=password,
    )

    flags: list[RouteHealthFlag] = []
    coverage: WindowCoverage | None = None
    for line in output.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row["kind"] == "coverage":
            if coverage is not None:
                raise ValueError("ClickHouse returned more than one coverage row")
            if row["oldest_created_at"] is None or row["newest_created_at"] is None:
                raise ValueError("ClickHouse provider_benchmark_samples is empty")
            coverage = WindowCoverage(
                oldest_created_at=_parse_clickhouse_datetime(row["oldest_created_at"]),
                newest_created_at=_parse_clickhouse_datetime(row["newest_created_at"]),
                table_rows=int(row["table_rows"]),
                window_rows=int(row["window_rows"]),
            )
            continue
        if row["kind"] != "flag":
            raise ValueError(f"unexpected ClickHouse result kind: {row['kind']!r}")
        flags.append(
            RouteHealthFlag(
                provider=str(row["provider"]),
                model=str(row["model"]),
                samples=int(row["samples"]),
                failures=int(row["failures"]),
                failure_rate=float(row["failure_rate"]),
                newest_error_type=row["newest_error_type"],
                newest_error_message=row["newest_error_message"],
            )
        )

    if coverage is None:
        raise ValueError("ClickHouse did not return its coverage row")
    return flags, coverage


def assert_window_covered(coverage: WindowCoverage, cutoff: dt.datetime) -> None:
    """Reject an empty or truncated ClickHouse evaluation window."""
    if coverage.table_rows <= 0:
        raise SystemExit("ClickHouse table is empty; refusing a trivial proof")
    if coverage.oldest_created_at > cutoff:
        raise SystemExit(
            "ClickHouse does not cover the requested window: "
            f"oldest row {coverage.oldest_created_at.isoformat()} is newer than "
            f"cutoff {cutoff.isoformat()}"
        )
    if coverage.window_rows <= 0 or coverage.newest_created_at < cutoff:
        raise SystemExit(
            "ClickHouse has no rows in the requested window: "
            f"newest row {coverage.newest_created_at.isoformat()} is older than "
            f"cutoff {cutoff.isoformat()}"
        )


def _by_route(flags: list[RouteHealthFlag], source: str) -> dict[tuple[str, str], RouteHealthFlag]:
    result: dict[tuple[str, str], RouteHealthFlag] = {}
    for flag in flags:
        key = (flag.provider, flag.model)
        if key in result:
            raise ValueError(f"{source} returned duplicate flag for {flag.provider}/{flag.model}")
        result[key] = flag
    return result


def _format_flag(flag: RouteHealthFlag | None) -> str:
    if flag is None:
        return "<not flagged>"
    return (
        f"samples={flag.samples} failures={flag.failures} "
        f"failure_rate={flag.failure_rate:.17g} "
        f"newest_error_type={flag.newest_error_type!r} "
        f"newest_error_message={flag.newest_error_message!r}"
    )


def compare_flags(
    python_result: list[RouteHealthFlag],
    clickhouse_result: list[RouteHealthFlag],
) -> int:
    python_by_route = _by_route(python_result, "Python")
    clickhouse_by_route = _by_route(clickhouse_result, "ClickHouse")
    mismatches = [
        route
        for route in sorted(set(python_by_route) | set(clickhouse_by_route))
        if python_by_route.get(route) != clickhouse_by_route.get(route)
    ]

    if mismatches:
        print(f"MISMATCH: {len(mismatches)} route(s)")
        for provider, model in mismatches:
            key = (provider, model)
            print(f"  {provider}/{model}")
            print(f"    Python:     {_format_flag(python_by_route.get(key))}")
            print(f"    ClickHouse: {_format_flag(clickhouse_by_route.get(key))}")
        return 1

    print(f"EXACT MATCH: {len(python_by_route)} flagged route(s)")
    for key in sorted(python_by_route):
        flag = python_by_route[key]
        print(f"  {flag.provider}/{flag.model}: {_format_flag(flag)}")
    return 0


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _unit_interval(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Differentially prove production route health against ClickHouse"
    )
    parser.add_argument("--window-hours", type=_positive_int, default=WINDOW_HOURS)
    parser.add_argument("--min-samples", type=_positive_int, default=MIN_SAMPLES)
    parser.add_argument(
        "--failure-threshold",
        type=_unit_interval,
        default=FAILURE_THRESHOLD,
    )
    parser.add_argument("--project", default=PROJECT)
    parser.add_argument("--bigtable-instance", default=BIGTABLE_INSTANCE)
    parser.add_argument("--bigtable-table", default=BIGTABLE_TABLE)
    args = parser.parse_args()

    password = os.environ.get("CH_PASSWORD")
    if not password:
        parser.error("CH_PASSWORD is required")

    routes = _production_routes()
    evaluated_at = _millisecond_now()
    cutoff = evaluated_at - dt.timedelta(hours=args.window_hours)

    print(
        f"Window: {cutoff.isoformat()} <= created_at "
        f"(evaluated at {evaluated_at.isoformat()})"
    )
    print(f"Routes compared: {len(routes)} production rotation candidates")

    print("Evaluating production Python route health against the real store...")
    production_result = python_flags(
        _real_store(args),
        routes=routes,
        evaluated_at=evaluated_at,
        window_hours=args.window_hours,
        min_samples=args.min_samples,
        failure_threshold=args.failure_threshold,
    )
    print(f"  Python flagged {len(production_result)} route(s)")

    print("Evaluating the same routes and window in one ClickHouse SQL query...")
    sql_result, coverage = clickhouse_flags(
        routes=routes,
        cutoff=cutoff,
        route_limit=SAMPLES_PER_ROUTE_LIMIT,
        min_samples=args.min_samples,
        failure_threshold=args.failure_threshold,
        password=password,
    )
    assert_window_covered(coverage, cutoff)
    print(f"  ClickHouse flagged {len(sql_result)} route(s)")
    print(
        "  ClickHouse data coverage: "
        f"{coverage.oldest_created_at.isoformat()} .. "
        f"{coverage.newest_created_at.isoformat()} "
        f"({coverage.window_rows}/{coverage.table_rows} rows at/after cutoff)"
    )

    return compare_flags(production_result, sql_result)


if __name__ == "__main__":
    raise SystemExit(main())
