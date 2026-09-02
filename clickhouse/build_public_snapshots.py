"""Precompute small public analytics payloads from bounded ClickHouse data."""

# ruff: noqa: S608
# Client rollup cutoffs are rendered only from typed UTC datetimes; every
# table name and limit is a module constant.

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import subprocess
from typing import Any

from trusted_router.apps import aggregate_apps
from trusted_router.catalog import MODELS, endpoints_for_model
from trusted_router.client_reliability import build_client_reliability
from trusted_router.storage_models import (
    ProviderBenchmarkSample,
    SyntheticProbeSample,
    SyntheticRollup,
)
from trusted_router.synthetic.leaderboard import aggregate_leaderboard
from trusted_router.synthetic.video_leaderboard import aggregate_video_leaderboard

SAMPLE_LIMIT = 10_000
PER_PROVIDER_LIMIT = 500
STATUS_SAMPLES_PER_DIMENSION = 30
STATUS_LIVE_SAMPLE_LIMIT = 5_000
STATUS_HOUR_ROLLUP_LIMIT = 5_000
VIDEO_SAMPLE_LIMIT = 5_000
CLIENT_ROLLUP_LIMIT = 100_000
# Published rows and calibration rows are fetched by separate queries, each
# with its own CLIENT_ROLLUP_LIMIT, so neither scope can truncate the other.
CLIENT_ROLLUP_SCOPES = ("fleet", "fleet_all")


def _query(
    password: str,
    sql: str,
    *,
    input_bytes: bytes | None = None,
    params: dict[str, str] | None = None,
) -> str:
    env = os.environ.copy()
    env["CLICKHOUSE_PASSWORD"] = password
    command = [
        "/usr/bin/clickhouse-client",
        "--user",
        "tr",
        "--database",
        "tr",
        "--query",
        sql,
    ]
    for key, value in sorted((params or {}).items()):
        command.append(f"--param_{key}={value}")
    result = subprocess.run(  # noqa: S603 - fixed executable and fixed SQL.
        command,
        input=input_bytes,
        env=env,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace")[:1000]
        raise RuntimeError(f"ClickHouse public snapshot query failed: {detail}")
    return result.stdout.decode()


def _dataclass_rows(cls: type[Any], output: str) -> list[Any]:
    allowed = {field.name for field in dataclasses.fields(cls)}
    rows: list[Any] = []
    for line in output.splitlines():
        raw = json.loads(line)
        if not isinstance(raw, dict):
            continue
        payload = {key: value for key, value in raw.items() if key in allowed}
        for key in ("streamed", "connection_reused", "output_match"):
            if payload.get(key) is not None:
                payload[key] = bool(payload[key])
        if cls is SyntheticRollup and payload.get("target_region") == "":
            payload["target_region"] = None
        for key in ("created_at", "period_start", "updated_at", "last_checked_at"):
            if payload.get(key) is not None:
                value = str(payload[key]).replace(" ", "T")
                payload[key] = value if value.endswith("Z") else value + "Z"
        rows.append(cls(**payload))
    return rows


def _clickhouse_string_array(values: list[str]) -> str:
    return "[" + ",".join(
        "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"
        for value in values
    ) + "]"


def _samples(password: str) -> list[ProviderBenchmarkSample]:
    output = _query(
        password,
        """
SELECT * EXCEPT (ingest_version, provider_rank)
FROM
(
  SELECT *, row_number() OVER (
    PARTITION BY provider ORDER BY created_at DESC, id DESC
  ) AS provider_rank
  FROM provider_benchmark_samples FINAL
  WHERE created_at >= now64(3) - INTERVAL 24 HOUR
)
WHERE provider_rank <= 500
ORDER BY created_at DESC, id DESC
LIMIT 10000
FORMAT JSONEachRow
""",
    )
    return _dataclass_rows(ProviderBenchmarkSample, output)


def _video_samples(password: str) -> list[ProviderBenchmarkSample]:
    model_ids = sorted(model.id for model in MODELS.values() if model.supports_video)
    if not model_ids:
        return []
    output = _query(
        password,
        """
SELECT * EXCEPT ingest_version
FROM provider_benchmark_samples FINAL
WHERE created_at >= now64(3) - INTERVAL 30 DAY
  AND model IN {models:Array(String)}
ORDER BY created_at DESC, id DESC
LIMIT 5000
FORMAT JSONEachRow
""",
        params={"models": _clickhouse_string_array(model_ids)},
    )
    return _dataclass_rows(ProviderBenchmarkSample, output)


def _status_inputs(
    password: str,
) -> tuple[list[SyntheticProbeSample], list[SyntheticRollup]]:
    samples = _dataclass_rows(
        SyntheticProbeSample,
        _query(
            password,
            f"""
SELECT * EXCEPT (ingest_version, probe_rank)
FROM
(
  SELECT *, row_number() OVER (
    PARTITION BY monitor_region, target, probe_type, target_region
    ORDER BY created_at DESC, id DESC
  ) AS probe_rank
  FROM synthetic_probe_samples FINAL
  WHERE created_at >= now64(3) - INTERVAL 1 HOUR
)
WHERE probe_rank <= {STATUS_SAMPLES_PER_DIMENSION}
ORDER BY created_at DESC, id DESC
LIMIT {STATUS_LIVE_SAMPLE_LIMIT}
FORMAT JSONEachRow
""",
        ),
    )
    rollups = _dataclass_rows(
        SyntheticRollup,
        _query(
            password,
            """
SELECT * EXCEPT ingest_version
FROM synthetic_status_rollups FINAL
WHERE period = 'hour'
  AND period_start >= toStartOfHour(now()) - INTERVAL 48 HOUR
ORDER BY period_start DESC, id DESC
LIMIT 5000
FORMAT JSONEachRow
""",
        ),
    )
    return samples, rollups


def _client_reliability_rows(
    password: str,
    *,
    now: dt.datetime,
    scope: str = "fleet",
) -> list[dict[str, Any]]:
    if scope not in CLIENT_ROLLUP_SCOPES:
        raise ValueError(f"unsupported client rollup scope: {scope}")
    cutoff_48h = (now - dt.timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_7d = (now - dt.timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
    cutoff_30d = (now - dt.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    output = _query(
        password,
        f"""
SELECT * EXCEPT computed_at
FROM client_availability_rollups FINAL
WHERE scope = '{scope}'
  AND (
    (period = '5m' AND period_start >= toDateTime('{cutoff_48h}', 'UTC'))
    OR (period = 'hour' AND period_start >= toDateTime('{cutoff_7d}', 'UTC'))
    OR (period = 'day' AND period_start >= toDateTime('{cutoff_30d}', 'UTC'))
  )
ORDER BY period_start DESC, period, id
LIMIT {CLIENT_ROLLUP_LIMIT}
FORMAT JSONEachRow
""",
    )
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def _client_reliability_signals(password: str, *, now: dt.datetime) -> dict[str, Any]:
    _ = now
    output = _query(
        password,
        """
SELECT
  maxIf(received_at, synthetic = 1)                                        AS canary_last_received_at,
  countIf(synthetic = 1 AND received_at >= now() - INTERVAL 24 HOUR)       AS canary_last_24h,
  max(received_at)                                                         AS newest_received_at
FROM client_minute_counters
WHERE received_at >= now() - INTERVAL 48 HOUR
FORMAT JSONEachRow
""",
    )
    for line in output.splitlines():
        if line.strip():
            row = json.loads(line)
            return row if isinstance(row, dict) else {}
    return {}


def _client_rows_by_window(
    rows: list[dict[str, Any]],
    *,
    now: dt.datetime,
) -> dict[str, list[dict[str, Any]]]:
    def parsed(row: dict[str, Any]) -> dt.datetime:
        value = dt.datetime.fromisoformat(
            str(row["period_start"]).replace(" ", "T").replace("Z", "+00:00")
        )
        return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)

    definitions = {
        "5m": ("5m", dt.timedelta(minutes=5)),
        "1h": ("5m", dt.timedelta(hours=1)),
        "24h": ("hour", dt.timedelta(hours=24)),
        "7d": ("hour", dt.timedelta(days=7)),
        "30d": ("day", dt.timedelta(days=30)),
        # Private alerting input: the three most recent buckets per host are
        # selected by the pure builder, even when delivery skipped a bucket.
        "watch_15m": ("5m", dt.timedelta(hours=48)),
    }
    return {
        name: [row for row in rows if row.get("period") == period and parsed(row) >= now - lookback]
        for name, (period, lookback) in definitions.items()
    }


def build_snapshots(
    samples: list[ProviderBenchmarkSample],
    *,
    generated_at: str,
    video_samples: list[ProviderBenchmarkSample] | None = None,
    status_samples: list[SyntheticProbeSample] | None = None,
    status_rollups: list[SyntheticRollup] | None = None,
    client_reliability_rows: list[dict[str, Any]] | None = None,
    client_reliability_signals: dict[str, Any] | None = None,
    client_reliability_all_traffic_rows: list[dict[str, Any]] | None = None,
) -> dict[str, dict[str, Any]]:
    leaderboard = aggregate_leaderboard(
        samples,
        min_samples=1,
        model_rank_min_samples=10,
        provider_rank_min_samples=30,
        rank_min_ttft_samples=3,
    )
    leaderboard.update(
        {
            "generated_at": generated_at,
            "sample_window_count": len(samples),
            "sample_limit": SAMPLE_LIMIT,
            "window_label": (
                f"rolling benchmark set of up to {SAMPLE_LIMIT:,} samples"
            ),
            "rank_minimums": {
                "model_availability_samples": 10,
                "provider_availability_samples": 30,
                "ttft_samples": 3,
            },
        }
    )
    apps = aggregate_apps(samples)
    apps["generated_at"] = generated_at
    video_rows = video_samples or []
    configured_video_routes = {
        (endpoint.provider, model.id)
        for model in MODELS.values()
        if model.supports_video
        for endpoint in endpoints_for_model(model.id)
    }
    video = aggregate_video_leaderboard(
        video_rows,
        configured_routes=configured_video_routes,
    )
    video.update(
        {
            "generated_at": generated_at,
            "sample_window_count": len(video_rows),
            "sample_limit": VIDEO_SAMPLE_LIMIT,
            "window_label": (
                f"rolling video benchmark set of up to {VIDEO_SAMPLE_LIMIT:,} jobs"
            ),
        }
    )
    status_inputs = {
        "generated_at": generated_at,
        "samples": [dataclasses.asdict(sample) for sample in status_samples or []],
        "rollups": [dataclasses.asdict(rollup) for rollup in status_rollups or []],
    }
    snapshot_now = dt.datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    client_reliability = build_client_reliability(
        _client_rows_by_window(client_reliability_rows or [], now=snapshot_now),
        snapshot_now,
        signals=client_reliability_signals,
        all_traffic_rows_by_window=(
            _client_rows_by_window(client_reliability_all_traffic_rows, now=snapshot_now)
            if client_reliability_all_traffic_rows is not None
            else None
        ),
    )
    return {
        "leaderboard": leaderboard,
        "apps": apps,
        "video_leaderboard": video,
        "status_inputs": status_inputs,
        "client_reliability": client_reliability,
    }


def main() -> int:
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    now = dt.datetime.now(dt.UTC)
    generated_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    status_samples, status_rollups = _status_inputs(password)
    snapshots = build_snapshots(
        _samples(password),
        generated_at=generated_at,
        video_samples=_video_samples(password),
        status_samples=status_samples,
        status_rollups=status_rollups,
        client_reliability_rows=_client_reliability_rows(password, now=now),
        client_reliability_signals=_client_reliability_signals(password, now=now),
        client_reliability_all_traffic_rows=_client_reliability_rows(
            password, now=now, scope="fleet_all"
        ),
    )
    rows = [
        {
            "name": name,
            "generated_at": generated_at,
            "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True),
            "ingest_version": now.isoformat(timespec="microseconds"),
        }
        for name, payload in snapshots.items()
    ]
    body = "\n".join(json.dumps(row, separators=(",", ":")) for row in rows).encode()
    _query(
        password,
        "INSERT INTO public_analytics_snapshots FORMAT JSONEachRow",
        input_bytes=body,
    )
    print(json.dumps({"snapshots": len(rows), "generated_at": generated_at}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
