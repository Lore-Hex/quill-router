from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from clickhouse.backfill_benchmark_samples import normalise

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
    _docker(docker, "run", "--rm", "-d", "--name", name, image)
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
            Path(__file__).parents[1] / "clickhouse" / "001_provider_benchmark_samples.sql"
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
    finally:
        _docker(docker, "rm", "-f", name, check=False)
