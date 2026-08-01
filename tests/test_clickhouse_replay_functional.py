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

            operational_schema = (
                Path(__file__).parents[1]
                / "clickhouse"
                / "004_operational_analytics_replicated.sql"
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
                input_bytes=operational_schema,
            )
            activity = {
                "generation_id": "gen-functional-replay",
                "request_id": "req-functional-replay",
                "tenant_id": "a" * 64,
                "key_id": "b" * 64,
                "model": "anthropic/claude-haiku-4.5",
                "provider": "anthropic",
                "provider_name": "Anthropic",
                "app": "Functional test",
                "tokens_prompt": 12,
                "tokens_completion": 3,
                "cached_input_tokens": 0,
                "reasoning_tokens": 0,
                "total_cost_microdollars": 9,
                "usage_type": "Credits",
                "speed_tokens_per_second": 7.5,
                "finish_reason": "stop",
                "status": "success",
                "streamed": 1,
                "usage_estimated": 0,
                "elapsed_milliseconds": 400,
                "first_token_milliseconds": 100,
                "ttfb_milliseconds": 20,
                "region": "us-central1",
                "user": None,
                "session_id": None,
                "http_referer": None,
                "app_categories": [],
                "tags": {},
                "created_at": "2026-07-28T12:34:56.789Z",
                "ingest_version": "2026-07-28T12:35:00.000001Z",
            }
            activity_payload = (json.dumps(activity) + "\n").encode()
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
                    "INSERT INTO activity_generations FORMAT JSONEachRow",
                    input_bytes=activity_payload,
                )
            activity_count = _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--query",
                "SELECT count() FROM activity_generations FINAL",
            )
            assert activity_count.stdout.decode().strip() == "1"
            sampled_activity = _docker(
                docker,
                "exec",
                "-i",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--external",
                "--file",
                "-",
                "--name",
                "wanted",
                "--structure",
                "id String",
                "--format",
                "TabSeparated",
                "--query",
                "SELECT generation_id FROM activity_generations FINAL "
                "WHERE generation_id IN (SELECT id FROM wanted) "
                "FORMAT JSONEachRow",
                input_bytes=b"gen-functional-replay\n",
            )
            assert json.loads(sampled_activity.stdout)["generation_id"] == (
                "gen-functional-replay"
            )

            synthetic = {
                "id": "synthetic-functional-replay",
                "probe_type": "tls_health",
                "target": "regional_api",
                "target_url": "https://api-us-central1.quillrouter.com/health",
                "monitor_region": "us-central1",
                "status": "up",
                "target_region": "us-central1",
                "latency_milliseconds": 10,
                "ttfb_milliseconds": 9,
                "dns_milliseconds": 1,
                "tcp_connect_milliseconds": 2,
                "tls_handshake_milliseconds": 3,
                "gateway_processing_milliseconds": 4,
                "connection_reused": 0,
                "protocol": "h2",
                "http_status": 200,
                "error_type": None,
                "provider": None,
                "model": None,
                "selected_provider": None,
                "selected_model": None,
                "generation_id": None,
                "attestation_digest": None,
                "source_commit": None,
                "cost_microdollars": 0,
                "output_match": 1,
                "created_at": "2026-07-28T12:34:56.789Z",
                "ingest_version": "2026-07-28T12:35:00.000001Z",
            }
            synthetic_payload = (json.dumps(synthetic) + "\n").encode()
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
                    "INSERT INTO synthetic_probe_samples FORMAT JSONEachRow",
                    input_bytes=synthetic_payload,
                )
            synthetic_count = _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--query",
                "SELECT count() FROM synthetic_probe_samples FINAL",
            )
            assert synthetic_count.stdout.decode().strip() == "1"

            replicated_rollup_schema = (
                Path(__file__).parents[1]
                / "clickhouse"
                / "005_provider_rollups_replicated.sql"
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
                input_bytes=replicated_rollup_schema,
            )
            rollup_engines = _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--query",
                "SELECT groupUniqArray(engine) FROM system.tables "
                "WHERE database=currentDatabase() "
                "AND name LIKE 'provider_analytics_%_replicated'",
            )
            assert rollup_engines.stdout.decode().strip() == "['ReplicatedMergeTree']"
            _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--query",
                "RENAME TABLE provider_analytics_hourly TO "
                "provider_analytics_hourly_local_backup, "
                "provider_analytics_hourly_replicated TO provider_analytics_hourly",
            )
            replicated_rollup_rows = recompute_partition(
                DockerExecutor(),
                RollupPartition(
                    "hourly",
                    dt.datetime(2026, 7, 28, tzinfo=dt.UTC),
                    dt.datetime(2026, 7, 29, tzinfo=dt.UTC),
                    "20260728",
                ),
            )
            assert replicated_rollup_rows == 1
            replicated_rollup_count = _docker(
                docker,
                "exec",
                name,
                "clickhouse-client",
                "--database",
                database,
                "--query",
                "SELECT sum(attempts) FROM provider_analytics_hourly",
            )
            assert replicated_rollup_count.stdout.decode().strip() == "1"
        finally:
            _docker(docker, "rm", "-f", name, check=False)
