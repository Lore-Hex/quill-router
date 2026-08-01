"""Precompute small public analytics payloads from bounded ClickHouse data."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import subprocess
from typing import Any

from trusted_router.apps import aggregate_apps
from trusted_router.storage_models import ProviderBenchmarkSample
from trusted_router.synthetic.leaderboard import aggregate_leaderboard

SAMPLE_LIMIT = 10_000
PER_PROVIDER_LIMIT = 500


def _query(password: str, sql: str, *, input_bytes: bytes | None = None) -> str:
    env = os.environ.copy()
    env["CLICKHOUSE_PASSWORD"] = password
    result = subprocess.run(  # noqa: S603 - fixed executable and fixed SQL.
        [
            "/usr/bin/clickhouse-client",
            "--user",
            "tr",
            "--database",
            "tr",
            "--query",
            sql,
        ],
        input=input_bytes,
        env=env,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.decode(errors="replace")[:1000]
        raise RuntimeError(f"ClickHouse public snapshot query failed: {detail}")
    return result.stdout.decode()


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
    allowed = {field.name for field in dataclasses.fields(ProviderBenchmarkSample)}
    samples: list[ProviderBenchmarkSample] = []
    for line in output.splitlines():
        raw = json.loads(line)
        if not isinstance(raw, dict):
            continue
        payload = {key: value for key, value in raw.items() if key in allowed}
        payload["streamed"] = bool(payload.get("streamed"))
        created_at = str(payload.get("created_at") or "").replace(" ", "T")
        if created_at and not created_at.endswith("Z"):
            created_at += "Z"
        payload["created_at"] = created_at
        samples.append(ProviderBenchmarkSample(**payload))
    return samples


def build_snapshots(
    samples: list[ProviderBenchmarkSample],
    *,
    generated_at: str,
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
    return {"leaderboard": leaderboard, "apps": apps}


def main() -> int:
    password = os.environ.get("CH_PASSWORD", "")
    if not password:
        raise SystemExit("CH_PASSWORD is required")
    now = dt.datetime.now(dt.UTC)
    generated_at = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    snapshots = build_snapshots(_samples(password), generated_at=generated_at)
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
