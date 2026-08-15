"""Recompute synthetic hour/day/month rollups from replicated raw rows."""

# ruff: noqa: S608
# Every SQL timestamp is rendered from a typed datetime and table names are
# module constants, so no request-controlled fragment reaches these queries.

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import json
import os
import subprocess
from typing import Any

from trusted_router.storage_models import SyntheticProbeSample, SyntheticRollup, iso_now
from trusted_router.synthetic.rollups import (
    apply_sample_to_rollup,
    new_rollup_for_sample,
    rollup_id,
    sample_rollup_ids,
)

RAW_TABLE = "synthetic_probe_samples"
ROLLUP_TABLE = "synthetic_status_rollups"


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


def fetch_samples(
    executor: ClickHouseExecutor,
    *,
    start: dt.datetime,
    end: dt.datetime,
) -> list[SyntheticProbeSample]:
    rows = executor.query(
        "SELECT * EXCEPT ingest_version FROM synthetic_probe_samples FINAL "
        f"WHERE created_at >= toDateTime64('{_ch_time(start)}', 3, 'UTC') "
        f"AND created_at < toDateTime64('{_ch_time(end)}', 3, 'UTC') "
        "ORDER BY created_at FORMAT JSONEachRow"
    )
    return [
        _sample_from_dict(json.loads(line))
        for line in rows.decode().splitlines()
        if line.strip()
    ]


def build_raw_rollups(
    samples: list[SyntheticProbeSample],
    *,
    periods: set[str],
) -> list[SyntheticRollup]:
    rollups: dict[str, SyntheticRollup] = {}
    for sample in _deduplicate_samples(samples):
        for period, component in sample_rollup_ids(sample):
            if period not in periods:
                continue
            update = new_rollup_for_sample(
                sample,
                period=period,
                component=component,
            )
            existing = rollups.get(update.id)
            if existing is None:
                rollups[update.id] = update
            else:
                apply_sample_to_rollup(existing, sample)
    return list(rollups.values())


def _deduplicate_samples(
    samples: list[SyntheticProbeSample],
) -> list[SyntheticProbeSample]:
    """Keep one latest version for each logical synthetic sample.

    Regional workers and at-least-once outbox delivery may emit the same sample
    ID more than once. Bigtable rollup markers already treat the ID as the
    identity, so ClickHouse rebuilds must do the same.
    """
    latest: dict[str, SyntheticProbeSample] = {}
    for sample in samples:
        existing = latest.get(sample.id)
        if existing is None or _sample_created_at(sample) >= _sample_created_at(existing):
            latest[sample.id] = sample
    return list(latest.values())


def _sample_created_at(sample: SyntheticProbeSample) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(sample.created_at.replace("Z", "+00:00"))
    return _utc(parsed)


def complete_window_rollups(
    rollups: list[SyntheticRollup],
    *,
    raw_start: dt.datetime,
) -> list[SyntheticRollup]:
    """Exclude the oldest partial period after raw TTL has started expiring."""
    safe_starts = {
        "hour": _ceil_period(raw_start, "hour"),
        "day": _ceil_period(raw_start, "day"),
    }
    result: list[SyntheticRollup] = []
    for rollup in rollups:
        safe_start = safe_starts.get(rollup.period)
        if safe_start is None:
            continue
        period_start = dt.datetime.fromisoformat(
            rollup.period_start.replace("Z", "+00:00")
        )
        if _utc(period_start) >= safe_start:
            result.append(rollup)
    return result


def fetch_daily_rollups(
    executor: ClickHouseExecutor,
    *,
    start: dt.datetime,
    end: dt.datetime,
) -> list[SyntheticRollup]:
    rows = executor.query(
        "SELECT * EXCEPT ingest_version FROM synthetic_status_rollups FINAL "
        "WHERE period = 'day' "
        f"AND period_start >= toDateTime('{_ch_time(start)}', 'UTC') "
        f"AND period_start < toDateTime('{_ch_time(end)}', 'UTC') "
        "FORMAT JSONEachRow"
    )
    return [
        _rollup_from_dict(json.loads(line))
        for line in rows.decode().splitlines()
        if line.strip()
    ]


def monthly_from_daily(daily: list[SyntheticRollup]) -> list[SyntheticRollup]:
    grouped: dict[tuple[str, ...], SyntheticRollup] = {}
    for source in daily:
        month_start = source.period_start[:7] + "-01T00:00:00Z"
        key = (
            month_start,
            source.component,
            source.target,
            source.probe_type,
            source.monitor_region,
            source.target_region or "",
        )
        target = grouped.get(key)
        if target is None:
            target = SyntheticRollup(
                id=rollup_id(
                    period="month",
                    period_start=month_start,
                    component=source.component,
                    target=source.target,
                    probe_type=source.probe_type,
                    monitor_region=source.monitor_region,
                    target_region=source.target_region,
                ),
                period="month",
                period_start=month_start,
                component=source.component,
                target=source.target,
                probe_type=source.probe_type,
                monitor_region=source.monitor_region,
                target_region=source.target_region,
            )
            grouped[key] = target
        _merge_rollup(target, source)
    return list(grouped.values())


def insert_rollups(
    executor: ClickHouseExecutor,
    rollups: list[SyntheticRollup],
    *,
    ingest_version: dt.datetime,
) -> None:
    if not rollups:
        return
    payload = b"\n".join(
        json.dumps(
            {
                **dataclasses.asdict(rollup),
                "target_region": rollup.target_region or "",
                "ingest_version": ingest_version.isoformat(),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        for rollup in rollups
    )
    executor.query(
        f"INSERT INTO {ROLLUP_TABLE} FORMAT JSONEachRow",
        input_bytes=payload,
    )


def recompute(executor: ClickHouseExecutor, *, now: dt.datetime) -> dict[str, int]:
    now = _utc(now)
    raw_start = now - dt.timedelta(days=14)
    samples = fetch_samples(executor, start=raw_start, end=now)
    raw_rollups = complete_window_rollups(
        build_raw_rollups(samples, periods={"hour", "day"}),
        raw_start=raw_start,
    )
    version = dt.datetime.now(dt.UTC)
    insert_rollups(executor, raw_rollups, ingest_version=version)

    previous_month = _month_start(_month_start(now) - dt.timedelta(days=1))
    next_month = _next_month(_month_start(now))
    daily = fetch_daily_rollups(executor, start=previous_month, end=next_month)
    monthly = monthly_from_daily(daily)
    insert_rollups(executor, monthly, ingest_version=version)
    return {
        "samples": len(samples),
        "hourly_daily_rollups": len(raw_rollups),
        "monthly_rollups": len(monthly),
    }


def _merge_rollup(target: SyntheticRollup, source: SyntheticRollup) -> None:
    for field in (
        "sample_count",
        "up_count",
        "down_count",
        "degraded_count",
        "routing_degraded_count",
        "trust_degraded_count",
        "unknown_count",
        "cost_microdollars",
    ):
        setattr(target, field, getattr(target, field) + getattr(source, field))
    for field in (
        "latency_histogram",
        "ttfb_histogram",
        "dns_histogram",
        "tcp_connect_histogram",
        "tls_handshake_histogram",
        "gateway_processing_histogram",
        "error_counts",
    ):
        destination = getattr(target, field)
        for key, count in getattr(source, field).items():
            destination[key] = destination.get(key, 0) + count
    if source.last_checked_at and (
        target.last_checked_at is None
        or source.last_checked_at > target.last_checked_at
    ):
        target.last_checked_at = source.last_checked_at
    target.updated_at = iso_now()


def _sample_from_dict(payload: dict[str, Any]) -> SyntheticProbeSample:
    payload = dict(payload)
    payload["created_at"] = _iso(payload["created_at"])
    for key in ("connection_reused", "output_match"):
        if payload.get(key) is not None:
            payload[key] = bool(payload[key])
    return SyntheticProbeSample(**payload)


def _rollup_from_dict(payload: dict[str, Any]) -> SyntheticRollup:
    payload = dict(payload)
    for key in ("period_start", "updated_at", "last_checked_at"):
        if payload.get(key) is not None:
            payload[key] = _iso(payload[key])
    if payload.get("target_region") == "":
        payload["target_region"] = None
    return SyntheticRollup(**payload)


def _iso(value: Any) -> str:
    text = str(value).replace(" ", "T")
    return text if text.endswith("Z") else text + "Z"


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def _month_start(value: dt.datetime) -> dt.datetime:
    return _utc(value).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _next_month(value: dt.datetime) -> dt.datetime:
    value = _month_start(value)
    return (value.replace(day=28) + dt.timedelta(days=4)).replace(day=1)


def _ceil_period(value: dt.datetime, period: str) -> dt.datetime:
    value = _utc(value)
    if period == "hour":
        floor = value.replace(minute=0, second=0, microsecond=0)
        return floor if value == floor else floor + dt.timedelta(hours=1)
    if period == "day":
        floor = value.replace(hour=0, minute=0, second=0, microsecond=0)
        return floor if value == floor else floor + dt.timedelta(days=1)
    raise ValueError(f"unsupported period: {period}")


def _ch_time(value: dt.datetime) -> str:
    return _utc(value).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--at", help="UTC ISO timestamp; defaults to now")
    args = parser.parse_args()
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    now = (
        dt.datetime.fromisoformat(args.at.replace("Z", "+00:00"))
        if args.at
        else dt.datetime.now(dt.UTC)
    )
    result = recompute(ClickHouseExecutor(password=password), now=now)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
