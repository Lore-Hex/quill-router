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

import httpx
import pytest

from clickhouse.benchmark_rows import normalise
from clickhouse.rollup_analytics import RollupPartition, recompute_partition
from trusted_router.operational_analytics import OperationalAnalyticsClient, _generation
from trusted_router.storage_activity import generation_events

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


def test_single_node_generation_projection_materializes_and_survives_merges() -> None:
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("docker executable is unavailable")
    name = f"tr-clickhouse-projection-{uuid.uuid4().hex[:12]}"
    image = os.environ.get("TR_CLICKHOUSE_TEST_IMAGE", "clickhouse/clickhouse-server:latest")
    root = Path(__file__).resolve().parents[1]
    helper = root / "scripts/deploy/_clickhouse_single_node_schema.sh"
    derived = subprocess.run(  # noqa: S603 - fixed helper and repository path
        ["bash", "-c", f'. "{helper}"; single_node_migrations "$1"', "_", str(root)],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    migrations = [Path(line) for line in derived.stdout.splitlines()]
    projection = root / "clickhouse/024_activity_generation_id_projection_single_node.sql"
    assert projection in migrations

    def query(sql: str, *, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        return _docker(
            docker,
            "exec",
            "-i",
            name,
            "clickhouse-client",
            "--database",
            "default",
            "--mutations_sync=1",
            "--multiquery",
            input_bytes=sql.encode(),
            check=check,
        )

    _docker(docker, "run", "--rm", "-d", "--name", name, image)
    try:
        for _ in range(60):
            if query("SELECT 1", check=False).returncode == 0:
                break
            time.sleep(0.25)
        else:
            pytest.fail("ClickHouse container did not become ready")

        for migration in migrations[:migrations.index(projection)]:
            query(migration.read_text())
        assert query(
            "SELECT engine FROM system.tables "
            "WHERE database = 'default' AND name = 'activity_generations'"
        ).stdout.decode().strip() == "ReplacingMergeTree"

        # The same ID under another sort key must survive FINAL as two rows.
        query(
            "INSERT INTO activity_generations "
            "(generation_id, tenant_id, created_at, ingest_version) "
            "SELECT concat('gen-projection-', toString(if(number < 32768, number, 7))), "
            "repeat(if(number = 32768, 'b', 'a'), 64), now64(3), now64(6) "
            "FROM numbers(32769)"
        )
        for _ in range(2):
            query(projection.read_text())
        for migration in migrations[migrations.index(projection) + 1:]:
            query(migration.read_text())

        def projection_rows() -> int:
            return int(query(
                "SELECT sum(rows) FROM system.projection_parts "
                "WHERE database = 'default' AND table = 'activity_generations' "
                "AND name = 'by_generation_id' AND active"
            ).stdout)

        assert projection_rows() == 32769
        lookup = (
            "SELECT tenant_id, created_at, generation_id FROM activity_generations "
            "WHERE generation_id = 'gen-projection-7' "
            "SETTINGS force_optimize_projection = 1 FORMAT JSONEachRow"
        )
        assert len(query(lookup).stdout.splitlines()) == 2

        # Keep a replay in a separate part until the explicit dedup merge.
        query("SYSTEM STOP MERGES activity_generations")
        query(
            "INSERT INTO activity_generations "
            "(generation_id, tenant_id, created_at, ingest_version) "
            "SELECT generation_id, tenant_id, created_at, now64(6) "
            "FROM activity_generations WHERE generation_id = 'gen-projection-7' "
            "AND tenant_id = repeat('a', 64)"
        )
        assert projection_rows() == 32770
        assert len(query(lookup).stdout.splitlines()) == 3
        assert query(
            "SELECT count() FROM activity_generations FINAL "
            "WHERE (tenant_id, created_at, generation_id) IN ("
            "SELECT tenant_id, created_at, generation_id FROM activity_generations "
            "WHERE generation_id = 'gen-projection-7')"
        ).stdout.decode().strip() == "2"

        query("SYSTEM START MERGES activity_generations")
        query("OPTIMIZE TABLE activity_generations FINAL")
        assert projection_rows() == 32769
        assert len(query(lookup).stdout.splitlines()) == 2

        refused = query(
            "DELETE FROM activity_generations WHERE generation_id = 'gen-projection-7'",
            check=False,
        )
        assert refused.returncode != 0
        assert b"Code: 344" in refused.stderr
        query(
            "ALTER TABLE activity_generations DELETE "
            "WHERE generation_id = 'gen-projection-7'"
        )
        assert projection_rows() == 32767
        assert query(lookup).stdout == b""
    finally:
        _docker(docker, "rm", "-f", name, check=False)


@pytest.mark.parametrize("standalone", [False, True], ids=["replicated", "single-node"])
def test_replayed_batch_collapses_under_final(standalone: bool) -> None:
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
  <remote_servers>
    <trustedrouter><shard><replica><host>127.0.0.1</host><port>9000</port></replica></shard></trustedrouter>
  </remote_servers>
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
                / (
                    "006_operational_analytics_single_node.sql" if standalone
                    else "004_operational_analytics_replicated.sql"
                )
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

            client_migration = (
                Path(__file__).parents[1] / "clickhouse" / (
                    "009_client_events_single_node.sql" if standalone
                    else "008_client_events_replicated.sql"
                )
            ).read_bytes()
            _docker(
                docker, "exec", "-i", name, "clickhouse-client",
                "--database", database, "--multiquery", input_bytes=client_migration,
            )

            app_migration = (
                Path(__file__).parents[1] / "clickhouse" / (
                    "026_activity_generation_app_fields_single_node.sql" if standalone
                    else "025_activity_generation_app_fields_replicated.sql"
                )
            ).read_text().replace("tr.activity_generations", f"{database}.activity_generations")
            for _ in range(2):
                _docker(
                    docker, "exec", "-i", name, "clickhouse-client",
                    "--database", database, "--multiquery",
                    input_bytes=app_migration.encode(),
                )

            route_migration = (
                Path(__file__).parents[1] / "clickhouse" / (
                    "028_activity_requested_model_single_node.sql" if standalone
                    else "027_activity_requested_model_replicated.sql"
                )
            ).read_text().replace("tr.activity_generations", f"{database}.activity_generations")
            for _ in range(2):
                _docker(
                    docker, "exec", "-i", name, "clickhouse-client",
                    "--database", database, "--multiquery", input_bytes=route_migration.encode(),
                )

            def read_activity() -> dict:
                result = _docker(
                    docker, "exec", name, "clickhouse-client", "--database", database,
                    "--query", "SELECT * FROM activity_generations FINAL FORMAT JSONEachRow",
                )
                [row] = [json.loads(line) for line in result.stdout.splitlines()]
                return row

            legacy = read_activity()
            assert legacy["requested_model"] == ""
            assert legacy["app_id"] is None
            assert int(legacy["app_markup_microdollars"]) == 0
            assert int(legacy["custom_model_markup_microdollars"]) == 0
            public = _generation(legacy, tenant_id=activity["tenant_id"]).to_openrouter_generation()
            assert public["app_id"] is None
            assert public["requested_model"] is None
            assert public["app_markup_microdollars"] == 0
            assert public["custom_model_markup_microdollars"] == 0

            activity.update(
                app_id="test-app", app_markup_microdollars=2, custom_model_markup_microdollars=3,
                requested_model="trustedrouter/auto",
                ingest_version="2026-07-28T12:35:01.000001Z",
            )
            for _ in range(2):
                _docker(
                    docker, "exec", "-i", name, "clickhouse-client",
                    "--database", database, "--query",
                    "INSERT INTO activity_generations FORMAT JSONEachRow",
                    input_bytes=(json.dumps(activity) + "\n").encode(),
                )
            stored = read_activity()
            assert "operator_cost_microdollars" not in stored
            generation = _generation(stored, tenant_id=activity["tenant_id"])
            public = generation.to_openrouter_generation()
            [event] = generation_events([generation])
            for field in ("app_id", "app_markup_microdollars", "custom_model_markup_microdollars", "requested_model"):
                assert public[field] == event[field] == activity[field]

            _docker(
                docker, "exec", name, "clickhouse-client", "--multiquery", "--query",
                "CREATE USER traffic_reader SETTINGS readonly=1, max_execution_time=30, "
                "max_memory_usage=1073741824; "
                f"GRANT SELECT ON {database}.activity_generations TO traffic_reader",
            )

            def traffic_query(request: httpx.Request) -> httpx.Response:
                args = [
                    f"--{key}={value}" for key, value in request.url.params.items()
                    if key.startswith("param_")
                ]
                result = _docker(
                    docker, "exec", "-i", name, "clickhouse-client", "--database", database,
                    "--user", "traffic_reader",
                    *args, "--query", request.content.decode(), check=False,
                )
                assert result.returncode == 0, result.stderr.decode()
                return httpx.Response(200, content=result.stdout)

            client = OperationalAnalyticsClient(
                base_url="http://clickhouse.test:8123", user="test", password="test",  # noqa: S106
                database=database, transport=httpx.MockTransport(traffic_query),
            )
            instant = dt.datetime.fromisoformat(activity["created_at"])
            assert client.routing_traffic(
                start_at=instant - dt.timedelta(days=1), end_at=instant,
            ) == []
            [traffic] = client.routing_traffic(
                start_at=instant, end_at=instant + dt.timedelta(days=1),
            )
            assert traffic["requested_model"] == "trustedrouter/auto"
            assert traffic["served_model"] == activity["model"]
            assert int(traffic["generations"]) == 1  # replay is not another request
            assert int(traffic["customer_charge_microdollars"]) == activity["total_cost_microdollars"]
            for excluded in (
                {**activity, "generation_id": "synthetic-activity", "synthetic": 1},
                {**activity, "generation_id": "failed-activity", "status": "failed"},
            ):
                _docker(
                    docker, "exec", "-i", name, "clickhouse-client", "--database", database,
                    "--query", "INSERT INTO activity_generations FORMAT JSONEachRow",
                    input_bytes=(json.dumps(excluded) + "\n").encode(),
                )
            assert client.routing_traffic(start_at=instant, end_at=instant + dt.timedelta(days=1)) == [traffic]
            _docker(
                docker, "exec", name, "clickhouse-client", "--query",
                "ALTER USER traffic_reader SETTINGS max_execution_time=0",
            )
            with pytest.raises(AssertionError, match="requires a bounded read-only profile"):
                client.routing_traffic(start_at=instant, end_at=instant + dt.timedelta(days=1))
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
                "created_at": dt.datetime.now(dt.UTC).isoformat(),
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
