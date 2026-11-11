"""Run the heartbeat-table migration against a fake gcloud; never contact a cloud."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/migrate_operational_outbox_heartbeat.sh"
TABLE = "tr_operational_outbox_heartbeat"

_FAKE_GCLOUD = '''
import json, os, re, sys
from pathlib import Path
state_file = Path(os.environ["FAKE_STATE"])
state = json.loads(state_file.read_text()) if state_file.exists() else {"calls": [], "tables": []}
args = sys.argv[1:]
state["calls"].append(args)
state_file.write_text(json.dumps(state))
if os.environ.get("FAIL_CHECK") == "1" and "execute-sql" in args:
    sys.exit(23)
if "execute-sql" in args:
    sql = next(arg for arg in args if arg.startswith("--sql="))
    name = re.search(r"table_name='([^']+)'", sql)[1]
    print(int(name in state["tables"]))
else:
    ddl = next(arg for arg in args if arg.startswith("--ddl="))
    name = re.search(r"CREATE TABLE (\\w+)", ddl)[1]
    assert name not in state["tables"], "duplicate table"
    state["tables"].append(name)
    state_file.write_text(json.dumps(state))
'''


def _migrate(tmp_path: Path, *, fail_check: bool = False) -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "gcloud"
    stub.write_text("#!" + sys.executable + "\n" + _FAKE_GCLOUD)
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "FAKE_STATE": str(tmp_path / "state.json"),
        "FAIL_CHECK": "1" if fail_check else "0",
        "GCP_PROJECT_ID": "test-project",
        "SPANNER_INSTANCE_ID": "test-instance",
        "SPANNER_DATABASE_ID": "test-database",
    }
    return subprocess.run(  # noqa: S603 - fixed repository script, isolated fake CLI
        ["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False,
    )


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state.json").read_text())


def test_the_migration_adds_the_table_once_and_is_idempotent(tmp_path: Path) -> None:
    first = _migrate(tmp_path)
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == f"created {TABLE}"
    second = _migrate(tmp_path)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == f"{TABLE} exists"

    state = _state(tmp_path)
    assert state["tables"] == [TABLE]
    # check, apply, check: the second run applies nothing.
    assert [("ddl" in call) for call in state["calls"]] == [False, True, False]
    for call in state["calls"]:
        assert "test-database" in call
        assert "--project=test-project" in call
        assert "--instance=test-instance" in call
    ddl = next(arg for arg in state["calls"][1] if arg.startswith("--ddl="))
    statement = " ".join(ddl.removeprefix("--ddl=").split())
    assert statement == (
        f"CREATE TABLE {TABLE} ( id STRING(64) NOT NULL, body STRING(MAX) NOT NULL, "
        "updated_at TIMESTAMP NOT NULL OPTIONS (allow_commit_timestamp=true), "
        ") PRIMARY KEY (id)"
    )


def test_a_schema_check_that_fails_stops_the_migration_without_applying(tmp_path: Path) -> None:
    """An unreadable schema is not an absent table: no blind apply."""
    result = _migrate(tmp_path, fail_check=True)

    assert result.returncode == 23
    state = _state(tmp_path)
    assert len(state["calls"]) == 1
    assert state["tables"] == []


def test_production_rollout_applies_it_with_the_sibling_migration_settings() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy-production.yml").read_text())
    steps = workflow["jobs"]["migrate-schema"]["steps"]
    runs = [step.get("run") for step in steps]
    added = runs.index("scripts/deploy/migrate_operational_outbox_heartbeat.sh")
    sibling = runs.index("scripts/deploy/migrate_operational_analytics_outbox.sh")
    assert added + 1 == sibling
    for field in ("env", "if", "shell", "continue-on-error"):
        assert steps[added].get(field) == steps[sibling].get(field)


def test_the_other_deploy_entry_points_apply_it_after_the_outbox() -> None:
    """The ClickHouse node deploy ships the writer; it must find the table."""
    for path in ("scripts/deploy-gcp.sh", "scripts/deploy/clickhouse_operational_analytics.sh"):
        script = (ROOT / path).read_text()
        outbox = script.index("migrate_operational_analytics_outbox.sh")
        heartbeat = script.index("migrate_operational_outbox_heartbeat.sh")
        assert outbox < heartbeat, path
    node_deploy = (ROOT / "scripts/deploy/clickhouse_operational_analytics.sh").read_text()
    assert node_deploy.index("migrate_operational_outbox_heartbeat.sh") < node_deploy.index(
        'build_clickhouse_bundle "$ROOT" "$archive"'
    )
