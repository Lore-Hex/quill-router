from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from clickhouse.backfill_benchmark_samples import normalise
from clickhouse.rollup_analytics import RollupPartition, recompute_partition

pytestmark = pytest.mark.skipif(
    os.environ.get("TR_RUN_CLICKHOUSE_FUNCTIONAL") != "1",
    reason="set TR_RUN_CLICKHOUSE_FUNCTIONAL=1 to run the Docker ClickHouse proof",
)


def _docker(
    executable: str,
    *args: str,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # noqa: S603 - resolved executable, fixed argv, no shell
        [executable, *args],
        input=input_bytes,
        capture_output=True,
        check=check,
    )


def test_replayed_batch_collapses_under_final() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker executable is unavailable")
    name = f"tr-clickhouse-replay-{uuid.uuid4().hex[:12]}"
    image = os.environ.get("TR_CLICKHOUSE_TEST_IMAGE", "clickhouse/clickhouse-server:latest")
    with tempfile.TemporaryDirectory(prefix="tr-clickhouse-keeper-") as temporary:
        keeper_config = Path(temporary) / "keeper.xml"
        keeper_config.write_text(
            """<clickhouse>
  <keeper_server>
    <tcp_port>9181</tcp_port>
    <server_id>1</server_id>
    <log_storage_path>/var/lib/clickhouse/coordination/log</log_storage_path>
    <snapshot_storage_path>/var/lib/clickhouse/coordination/snapshots</snapshot_storage_path>
    <raft_configuration>
      <server><id>1</id><hostname>127.0.0.1</hostname><port>9234</port></server>
    </raft_configuration>
  </keeper_server>
  <zookeeper><node><host>127.0.0.1</host><port>9181</port></node></zookeeper>
  <macros><shard>01</shard><replica>functional-1</replica></macros>
</clickhouse>
"""
        )
        _docker(
            docker,
            "run",
            "--rm",
            "-d",
            "--name",
            name,
            "-v",
            f"{keeper_config}:/etc/clickhouse-server/config.d/keeper.xml:ro",
            image,
        )
        try:
            for _ in range(60):
                ready = _docker(
                    docker,
                    "exec",
                    name,
                    "clickhouse-client",
                    "--query",
                    "SELECT 1",
                    check=False,
                )
                if ready.returncode == 0:
                    break
                time.sleep(0.25)
            else:
                pytest.fail("ClickHouse container did not become ready")

            database = f"replay_{uuid.uuid4().hex}"
            _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--query",
                f"CREATE DATABASE {database}",
            )
            schema = (
                Path(__file__).parents[1]
                / "clickhouse"
                / "001_provider_benchmark_samples.sql"
            ).read_bytes()
            _docker(
                docker,
                "exec",
                "-i",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--multiquery",
                input_bytes=schema,
            )

            raw = {
                "id": "bench-functional-replay",
                "created_at": "2026-07-28T12:34:56.789Z",
                "provider": "anthropic",
                "model": "anthropic/claude-haiku-4.5",
                "provider_name": "Anthropic",
                "status": "success",
                "usage_type": "Credits",
                "source": "organic",
                "streamed": True,
                "input_tokens": 12,
                "output_tokens": 3,
                "total_cost_microdollars": 9,
            }
            canonical = normalise(raw)
            assert canonical is not None
            payload = (json.dumps(canonical) + "\n").encode()
            for _ in range(2):
                _docker(
                    docker,
                    "exec",
                    "-i",
                    name,
                    "clickhouse-client",
                    "--database",
                    database,
                    "--query",
                    "INSERT INTO provider_benchmark_samples FORMAT JSONEachRow",
                    input_bytes=payload,
                )
            count = _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--query",
                "SELECT count() FROM provider_benchmark_samples FINAL",
            )
            assert count.stdout.decode().strip() == "1"

            rollup_schema = (
                Path(__file__).parents[1]
                / "clickhouse"
                / "002_provider_analytics_rollups.sql"
            ).read_bytes()
            _docker(
                docker,
                "exec",
                "-i",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--multiquery",
                input_bytes=rollup_schema,
            )

            class DockerExecutor:
                def execute(self, query: str) -> bytes:
                    result = _docker(
                        docker,
                        "exec",
                        name,
                        "clickhouse-client",
                        "--database",
                        database,
                        "--query",
                        query,
                    )
                    return result.stdout

            rolled = recompute_partition(
                DockerExecutor(),
                RollupPartition(
                    "hourly",
                    dt.datetime(2026, 7, 28, tzinfo=dt.UTC),
                    dt.datetime(2026, 7, 29, tzinfo=dt.UTC),
                    "20260728",
                ),
            )
            assert rolled == 1
            rollup_count = _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--query",
                "SELECT sum(attempts) FROM provider_analytics_hourly",
            )
            assert rollup_count.stdout.decode().strip() == "1"

            daily_ddl = _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--query",
                "SHOW CREATE TABLE provider_analytics_daily",
            ).stdout.decode()
            hourly_ddl = _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--query",
                "SHOW CREATE TABLE provider_analytics_hourly",
            ).stdout.decode()
            assert "TTL" not in daily_ddl
            assert "TTL" in hourly_ddl

            replicated_schema = (
                Path(__file__).parents[1]
                / "clickhouse"
                / "003_provider_benchmark_replicated.sql"
            ).read_bytes()
            _docker(
                docker,
                "exec",
                "-i",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--multiquery",
                input_bytes=replicated_schema,
            )
            engine = _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--query",
                "SELECT engine FROM system.tables "
                "WHERE database=currentDatabase() "
                "AND name='provider_benchmark_samples_replicated'",
            )
            assert engine.stdout.decode().strip() == "ReplicatedReplacingMergeTree"
        finally:
            _docker(docker, "rm", "-f", name, check=False)
