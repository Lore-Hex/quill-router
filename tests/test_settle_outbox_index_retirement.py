from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/deploy/retire_settle_outbox_hot_index.sh"


def _fake_gcloud(tmp_path: Path) -> tuple[Path, Path]:
    marker = tmp_path / "ddl.txt"
    executable = tmp_path / "gcloud"
    executable.write_text(
        """#!/usr/bin/env python3
import os
from pathlib import Path
import sys

args = " ".join(sys.argv[1:])
if "execute-sql" in args:
    if "tr_settle_outbox_due_v2" in args and "INFORMATION_SCHEMA.INDEXES" in args:
        print(os.environ.get("FAKE_V2_COUNT", "1"))
    elif "queue_shard IS NULL" in args:
        print(os.environ.get("FAKE_UNSHARDED", "0"))
    elif "FORCE_INDEX=tr_settle_outbox_due_v2" in args:
        print("0")
    elif "tr_settle_outbox_due" in args and "INFORMATION_SCHEMA.INDEXES" in args:
        print(os.environ.get("FAKE_LEGACY_COUNT", "1"))
    else:
        raise SystemExit(f"unexpected execute-sql: {args}")
elif "databases ddl update" in args:
    Path(os.environ["FAKE_DDL_MARKER"]).write_text(args)
else:
    raise SystemExit(f"unexpected gcloud command: {args}")
"""
    )
    executable.chmod(0o755)
    return executable, marker


def _run(tmp_path: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
    _fake_gcloud(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "SPANNER_INSTANCE_ID": "instance",
        "SPANNER_DATABASE_ID": "database",
        "GCP_PROJECT_ID": "project",
        "FAKE_DDL_MARKER": str(tmp_path / "ddl.txt"),
        **overrides,
    }
    return subprocess.run(  # noqa: S603 - executes the checked-in deploy script.
        [str(SCRIPT)],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )


def test_retirement_drops_legacy_index_after_all_guards_pass(tmp_path: Path) -> None:
    result = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    ddl = (tmp_path / "ddl.txt").read_text()
    assert "--ddl=DROP INDEX tr_settle_outbox_due" in ddl


def test_retirement_refuses_when_v2_is_not_ready(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_V2_COUNT="0")

    assert result.returncode != 0
    assert "is not ready" in result.stdout
    assert not (tmp_path / "ddl.txt").exists()


def test_retirement_refuses_unsharded_pending_rows(tmp_path: Path) -> None:
    result = _run(tmp_path, FAKE_UNSHARDED="2")

    assert result.returncode != 0
    assert "pending rows have no generated queue shard" in result.stdout
    assert not (tmp_path / "ddl.txt").exists()


def test_retirement_is_idempotent_when_legacy_index_is_absent(
    tmp_path: Path,
) -> None:
    result = _run(tmp_path, FAKE_LEGACY_COUNT="0")

    assert result.returncode == 0
    assert "legacy index already absent" in result.stdout
    assert not (tmp_path / "ddl.txt").exists()
