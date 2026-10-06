from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from clickhouse import build_public_snapshots as worker


@pytest.mark.skipif(
    os.environ.get("TR_RUN_CLICKHOUSE_FUNCTIONAL") != "1",
    reason="set TR_RUN_CLICKHOUSE_FUNCTIONAL=1 to run the Docker ClickHouse proof",
)
def test_key_ranking_matches_full_row_ranking_with_replay_and_sampling_limits(monkeypatch):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker executable is unavailable")
    queries = []
    monkeypatch.setattr(worker, "_query", lambda password, query: queries.append(query) or "")
    worker._evidence_samples("unused")
    original = """
SELECT * EXCEPT (ingest_version, route_rank, provider_rank)
FROM (
  SELECT *, row_number() OVER (
    PARTITION BY provider ORDER BY route_rank, created_at DESC, id DESC
  ) AS provider_rank
  FROM (
    SELECT *, row_number() OVER (
      PARTITION BY provider, model, source ORDER BY created_at DESC, id DESC
    ) AS route_rank
    FROM provider_benchmark_samples FINAL
    WHERE created_at >= now64(3) - INTERVAL 7 DAY
  ) WHERE route_rank <= 30
) WHERE provider_rank <= 500
ORDER BY provider_rank, created_at DESC, id DESC
LIMIT 10000
FORMAT JSONEachRow;
"""
    fixture = """
CREATE TABLE provider_benchmark_samples (
  provider String, model String, source String, created_at DateTime64(3),
  id String, status String, error_message String, total_cost_microdollars Int64,
  ingest_version UInt64
) ENGINE = ReplacingMergeTree(ingest_version)
ORDER BY (provider, model, created_at, id);
INSERT INTO provider_benchmark_samples
SELECT concat('provider-', toString(intDiv(number, 2000))),
       concat('model-', toString(number % 20)),
       if(intDiv(number, 20) % 2 = 0, 'organic', 'synthetic'),
       toDateTime64('2026-09-30 00:00:00', 3) - INTERVAL (number % 60) SECOND
         - INTERVAL (if(number % 13 = 0, 8, 0)) DAY,
       toString(number), if(number % 3 = 0, 'error', 'success'),
       repeat('fixture detail ', 8), number % 17, 1
FROM numbers(60000);
INSERT INTO provider_benchmark_samples
SELECT provider, model, source, created_at, id, 'error', 'latest replay',
       total_cost_microdollars + 1, 2
FROM provider_benchmark_samples WHERE toUInt64(id) % 19 = 0;
"""
    # Freeze both queries at the same cutoff, including old rows and timestamp ties.
    cutoff = "toDateTime64('2026-09-30 12:00:00', 3)"
    sql = fixture + original.replace("now64(3)", cutoff)
    sql += queries[0].replace("now64(3)", cutoff) + ";"
    result = subprocess.run(  # noqa: S603 - fixed local, network-isolated test container.
        [
            docker, "run", "--rm", "-i", "--network", "none", "--cpus", "1",
            "--memory", "1g", "--entrypoint", "clickhouse",
            os.environ.get("TR_CLICKHOUSE_TEST_IMAGE", "clickhouse/clickhouse-server:latest"),
            "local", "--multiquery",
        ],
        input=sql.encode(), capture_output=True, check=True, timeout=120,
    )
    rows = [json.loads(line) for line in result.stdout.splitlines()]
    expected, actual = rows[:10000], rows[10000:]
    assert len(expected) == len(actual) == 10000
    assert actual == expected
    assert {row["source"] for row in actual} == {"organic", "synthetic"}
    assert {row["status"] for row in actual} == {"error", "success"}
    assert any(row["error_message"] == "latest replay" for row in actual)
