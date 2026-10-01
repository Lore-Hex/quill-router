"""Query the node-local ClickHouse through ``clickhouse-client``."""

from __future__ import annotations

import os
import subprocess

DATABASE = "tr"


class ClickHouse:
    def __init__(self, *, password: str) -> None:
        self._password = password

    def query(
        self,
        sql: str,
        *,
        input_bytes: bytes | None = None,
        external_ids: bool = False,
    ) -> str:
        env = os.environ.copy()
        env["CLICKHOUSE_PASSWORD"] = self._password
        command = [
            "/usr/bin/clickhouse-client",
            "--user",
            "tr",
            "--database",
            DATABASE,
        ]
        if external_ids:
            command.extend(
                [
                    "--external",
                    "--file",
                    "-",
                    "--name",
                    "wanted",
                    "--structure",
                    "id String",
                    "--format",
                    "TabSeparated",
                ]
            )
        command.extend(["--query", sql])
        result = subprocess.run(  # noqa: S603 - fixed executable and SQL below.
            command,
            input=input_bytes,
            env=env,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.decode(errors="replace")[:1000]
            raise RuntimeError(f"ClickHouse query failed: {detail}")
        return result.stdout.decode()
