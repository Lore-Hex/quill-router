"""Recompute verified hourly, daily, and monthly ClickHouse rollups."""

# ruff: noqa: S608
# SQL identifiers are allowlist-validated and all other fragments are generated
# from typed UTC boundaries and a closed granularity allowlist.

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import logging
import os
import re
import subprocess
from collections.abc import Iterable, Sequence
from typing import Protocol

DATABASE = "tr"
RAW_TABLE = "provider_benchmark_samples"
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_GRANULARITIES = ("hourly", "daily", "monthly")

log = logging.getLogger("trusted_router.analytics_rollup")


def _identifier(value: str, *, label: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} must be a ClickHouse identifier")
    return value


def _sql_datetime(value: dt.datetime) -> str:
    utc = value.astimezone(dt.UTC)
    text = utc.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return f"toDateTime64('{text}', 3, 'UTC')"


def _month_start(value: dt.date) -> dt.date:
    return value.replace(day=1)


def _shift_month(value: dt.date, months: int) -> dt.date:
    absolute = value.year * 12 + value.month - 1 + months
    return dt.date(absolute // 12, absolute % 12 + 1, 1)


@dataclasses.dataclass(frozen=True)
class RollupPartition:
    granularity: str
    start: dt.datetime
    end: dt.datetime
    partition_id: str

    def __post_init__(self) -> None:
        if self.granularity not in _GRANULARITIES:
            raise ValueError(f"unsupported granularity: {self.granularity}")
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("rollup boundaries must be timezone-aware")
        if self.end < self.start:
            raise ValueError("rollup end cannot precede start")


class QueryExecutor(Protocol):
    def execute(self, query: str) -> bytes: ...


class ClickHouseExecutor:
    def __init__(self, *, password: str, database: str = DATABASE) -> None:
        self._password = password
        self._database = _identifier(database, label="database")

    def execute(self, query: str) -> bytes:
        env = os.environ.copy()
        env["CLICKHOUSE_PASSWORD"] = self._password
        result = subprocess.run(  # noqa: S603 - fixed executable and argv, no shell
            [
                "/usr/bin/clickhouse-client",
                "--user",
                "tr",
                "--database",
                self._database,
                "--multiquery",
                "--query",
                query,
            ],
            env=env,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"ClickHouse rollup query failed: {detail}")
        return result.stdout


def planned_partitions(
    granularity: str,
    *,
    now: dt.datetime,
    backfill_start: dt.date | None = None,
) -> list[RollupPartition]:
    if granularity not in _GRANULARITIES:
        raise ValueError(f"unsupported granularity: {granularity}")
    now = now.astimezone(dt.UTC)
    today = now.date()
    if granularity == "hourly":
        first = backfill_start or today - dt.timedelta(days=2)
        dates = _date_range(first, today)
        result: list[RollupPartition] = []
        closed_hour = now.replace(minute=0, second=0, microsecond=0)
        for day in dates:
            start = dt.datetime.combine(day, dt.time(), tzinfo=dt.UTC)
            end = min(start + dt.timedelta(days=1), closed_hour)
            if end > start:
                result.append(RollupPartition("hourly", start, end, day.strftime("%Y%m%d")))
        return result

    current_month = _month_start(today)
    first_month = _month_start(backfill_start) if backfill_start else _shift_month(current_month, -1)
    months = _month_range(first_month, current_month)
    if granularity == "daily":
        start_of_today = dt.datetime.combine(today, dt.time(), tzinfo=dt.UTC)
        return [
            RollupPartition(
                "daily",
                dt.datetime.combine(month, dt.time(), tzinfo=dt.UTC),
                min(
                    dt.datetime.combine(_shift_month(month, 1), dt.time(), tzinfo=dt.UTC),
                    start_of_today,
                ),
                month.strftime("%Y%m"),
            )
            for month in months
            if dt.datetime.combine(month, dt.time(), tzinfo=dt.UTC) < start_of_today
        ]

    # Monthly rows are published only after a complete calendar month closes.
    complete_months = [month for month in months if month < current_month]
    return [
        RollupPartition(
            "monthly",
            dt.datetime.combine(month, dt.time(), tzinfo=dt.UTC),
            dt.datetime.combine(_shift_month(month, 1), dt.time(), tzinfo=dt.UTC),
            month.strftime("%Y%m"),
        )
        for month in complete_months
    ]


def _date_range(first: dt.date, last: dt.date) -> list[dt.date]:
    if first > last:
        return []
    return [first + dt.timedelta(days=offset) for offset in range((last - first).days + 1)]


def _month_range(first: dt.date, last: dt.date) -> list[dt.date]:
    if first > last:
        return []
    result: list[dt.date] = []
    value = first
    while value <= last:
        result.append(value)
        value = _shift_month(value, 1)
    return result


def _bucket_expression(granularity: str) -> str:
    return {
        "hourly": "toStartOfHour(created_at, 'UTC')",
        "daily": "toStartOfDay(created_at, 'UTC')",
        "monthly": "toStartOfMonth(created_at, 'UTC')",
    }[granularity]


def _nullable_quantile(column: str, quantile: float) -> str:
    return (
        f"if(countIf({column} IS NOT NULL) = 0, NULL, "
        f"quantileTDigestIf({quantile})({column}, {column} IS NOT NULL))"
    )


def _rollup_select(
    partition: RollupPartition,
    *,
    raw_table: str,
) -> str:
    raw_table = _identifier(raw_table, label="raw table")
    bucket = _bucket_expression(partition.granularity)
    where = (
        f"created_at >= {_sql_datetime(partition.start)} "
        f"AND created_at < {_sql_datetime(partition.end)}"
    )
    return f"""
SELECT
  {bucket} AS period_start,
  provider,
  model,
  source,
  ifNull(region, '') AS region,
  usage_type,
  status,
  ifNull(error_type, '') AS error_type,
  ifNull(error_status, 0) AS error_status,
  streamed,
  count() AS attempts,
  countIf(status = 'success') AS completed,
  countIf(status != 'success') AS failed,
  sum(toUInt64(input_tokens)) AS input_tokens,
  sum(toUInt64(output_tokens)) AS output_tokens,
  sum(total_cost_microdollars) AS total_cost_microdollars,
  {_nullable_quantile('elapsed_milliseconds', 0.50)} AS p50_elapsed_milliseconds,
  {_nullable_quantile('elapsed_milliseconds', 0.95)} AS p95_elapsed_milliseconds,
  {_nullable_quantile('first_token_milliseconds', 0.50)} AS p50_first_token_milliseconds,
  {_nullable_quantile('first_token_milliseconds', 0.95)} AS p95_first_token_milliseconds,
  {_nullable_quantile('ttfb_milliseconds', 0.50)} AS p50_ttfb_milliseconds,
  {_nullable_quantile('ttfb_milliseconds', 0.95)} AS p95_ttfb_milliseconds,
  {_nullable_quantile('speed_tokens_per_second', 0.50)} AS p50_tokens_per_second,
  {_nullable_quantile('speed_tokens_per_second', 0.95)} AS p95_tokens_per_second
FROM {raw_table} FINAL
WHERE {where}
GROUP BY
  period_start, provider, model, source, region, usage_type, status,
  error_type, error_status, streamed
""".strip()


def _scalar_uint(payload: bytes) -> int:
    text = payload.decode("utf-8").strip()
    if not text:
        raise RuntimeError("ClickHouse scalar query returned no value")
    return int(text.splitlines()[-1])


def recompute_partition(
    executor: QueryExecutor,
    partition: RollupPartition,
    *,
    raw_table: str = RAW_TABLE,
) -> int:
    target = f"provider_analytics_{partition.granularity}"
    staging = f"{target}_staging"
    select = _rollup_select(partition, raw_table=raw_table)
    where = (
        f"created_at >= {_sql_datetime(partition.start)} "
        f"AND created_at < {_sql_datetime(partition.end)}"
    )
    source_rows = _scalar_uint(
        executor.execute(f"SELECT count() FROM {_identifier(raw_table, label='raw table')} FINAL WHERE {where}")
    )
    executor.execute(f"TRUNCATE TABLE {staging}")
    if source_rows == 0:
        executor.execute(f"ALTER TABLE {target} DROP PARTITION ID '{partition.partition_id}'")
        return 0
    executor.execute(f"INSERT INTO {staging} {select}")
    staged_rows = _scalar_uint(executor.execute(f"SELECT sum(attempts) FROM {staging}"))
    if staged_rows != source_rows:
        raise RuntimeError(
            f"{partition.granularity} rollup parity mismatch for {partition.partition_id}: "
            f"source={source_rows} staged={staged_rows}"
        )
    executor.execute(
        f"ALTER TABLE {target} REPLACE PARTITION ID '{partition.partition_id}' FROM {staging}"
    )
    live_rows = _scalar_uint(
        executor.execute(
            f"SELECT sum(attempts) FROM {target} "
            f"WHERE _partition_id = '{partition.partition_id}'"
        )
    )
    if live_rows != source_rows:
        raise RuntimeError(
            f"{partition.granularity} live parity mismatch for {partition.partition_id}: "
            f"source={source_rows} live={live_rows}"
        )
    return source_rows


def _raw_start(executor: QueryExecutor, raw_table: str) -> dt.date | None:
    table = _identifier(raw_table, label="raw table")
    payload = executor.execute(
        f"SELECT if(count() = 0, '', toString(toDate(min(created_at)))) FROM {table} FINAL"
    )
    value = payload.decode("utf-8").strip().splitlines()[-1]
    return dt.date.fromisoformat(value) if value else None


def run_rollups(
    executor: QueryExecutor,
    granularities: Iterable[str],
    *,
    now: dt.datetime,
    raw_table: str = RAW_TABLE,
    backfill: bool = False,
) -> list[tuple[RollupPartition, int]]:
    start = _raw_start(executor, raw_table) if backfill else None
    if backfill and start is None:
        return []
    results: list[tuple[RollupPartition, int]] = []
    for granularity in granularities:
        for partition in planned_partitions(
            granularity,
            now=now,
            backfill_start=start,
        ):
            rows = recompute_partition(executor, partition, raw_table=raw_table)
            results.append((partition, rows))
    return results


def _parse_granularities(value: str) -> Sequence[str]:
    if value == "all":
        return _GRANULARITIES
    if value not in _GRANULARITIES:
        raise argparse.ArgumentTypeError("granularity must be hourly, daily, monthly, or all")
    return (value,)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", default=DATABASE)
    parser.add_argument("--raw-table", default=RAW_TABLE)
    parser.add_argument("--granularity", default="all", type=_parse_granularities)
    parser.add_argument("--backfill", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    executor = ClickHouseExecutor(
        password=os.environ["CH_PASSWORD"],
        database=args.database,
    )
    results = run_rollups(
        executor,
        args.granularity,
        now=dt.datetime.now(dt.UTC),
        raw_table=args.raw_table,
        backfill=args.backfill,
    )
    for partition, rows in results:
        log.info(
            "analytics_rollup.completed granularity=%s partition=%s start=%s end=%s rows=%d",
            partition.granularity,
            partition.partition_id,
            partition.start.isoformat(),
            partition.end.isoformat(),
            rows,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
