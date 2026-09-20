"""Real ClickHouse semantics for bounded leaderboard selection, local only."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from collections import Counter

import pytest

from clickhouse import build_public_snapshots as worker

pytestmark = pytest.mark.skipif(
    os.environ.get("TR_RUN_CLICKHOUSE_FUNCTIONAL") != "1",
    reason="set TR_RUN_CLICKHOUSE_FUNCTIONAL=1 for the local Docker SQL proof",
)


def test_narrow_evidence_selection_matches_previous_membership(monkeypatch) -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker is unavailable")
    name = f"tr-evidence-{uuid.uuid4().hex[:12]}"

    def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed local Docker executable and argv.
            [docker, *args], capture_output=True, text=True, check=check, timeout=60
        )

    def query(sql: str) -> str:
        return run("exec", name, "clickhouse-client", "--query", sql).stdout

    queries: list[str] = []
    monkeypatch.setattr(worker, "_query", lambda password, sql: queries.append(sql) or "")
    worker._evidence_samples("unused")
    sql = queries[0]
    run("run", "--rm", "-d", "--network=none", "--memory=768m", "--cpus=2",
        "--name", name, os.environ.get("TR_CLICKHOUSE_TEST_IMAGE", "clickhouse/clickhouse-server:latest"))
    try:
        for _ in range(60):
            if run("exec", name, "clickhouse-client", "--query", "SELECT 1", check=False).returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.fail("local ClickHouse did not become ready")
        query("""CREATE TABLE provider_benchmark_samples (
            id String, provider String, model String, source String,
            created_at DateTime64(3, 'UTC'), status String,
            error_message String, ingest_version UInt64
        ) ENGINE=ReplacingMergeTree(ingest_version)
        ORDER BY (provider, model, created_at, id)""")
        query("""INSERT INTO provider_benchmark_samples
        SELECT toString(number), toString(intDiv(number, 1400)),
               toString(intDiv(number, 70) % 20),
               if(intDiv(number, 35) % 2 = 0, 'organic', 'synthetic'),
               now64(3) - INTERVAL 1 HOUR + toIntervalSecond(number % 35),
               if(number % 3 = 0, 'error', 'success'), repeat('metadata', 32), 1
        FROM numbers(32200)""")
        # At-least-once replay must not inflate the selected population.
        query("INSERT INTO provider_benchmark_samples SELECT * FROM provider_benchmark_samples")
        baseline = query("""
        SELECT id, provider, model, source, created_at, status, error_message
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
        ORDER BY provider_rank, created_at DESC, id DESC LIMIT 10000
        FORMAT JSONEachRow
        """)
        actual = [json.loads(line) for line in query(sql).splitlines()]
        expected = [json.loads(line) for line in baseline.splitlines()]
        assert {row["id"]: row for row in actual} == {row["id"]: row for row in expected}
        assert len(actual) == 10000
        assert max(Counter(row["provider"] for row in actual).values()) <= 500
        assert max(Counter((row["provider"], row["model"], row["source"]) for row in actual).values()) <= 30
        assert {row["status"] for row in actual} == {"success", "error"}
    finally:
        run("rm", "-f", name, check=False)
