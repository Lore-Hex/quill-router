"""Run the ClickHouse node role migration against a fake gcloud; never contact a cloud."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/migrate_clickhouse_node_role.sh"
VIEW = "tr_workspace_directory_source"
ROLE = "tr_clickhouse_node"

_FAKE_GCLOUD = '''
import json, os, re, sys
from pathlib import Path
state_file = Path(os.environ["FAKE_STATE"])
state = json.loads(state_file.read_text()) if state_file.exists() else {
    "calls": [], "views": [], "roles": [], "grants": []}
args = sys.argv[1:]
state["calls"].append(args)
state_file.write_text(json.dumps(state))
fail = os.environ.get("FAIL_CHECK", "")
if "execute-sql" in args:
    sql = next(arg for arg in args if arg.startswith("--sql="))
    if "INFORMATION_SCHEMA.VIEWS" in sql:
        if fail == "views":
            sys.exit(23)
        name = re.search(r"table_name='([^']+)'", sql)[1]
        print(int(name in state["views"]))
    else:
        if fail == "roles":
            sys.exit(24)
        name = re.search(r"role_name='([^']+)'", sql)[1]
        print(int(name in state["roles"]))
else:
    ddl = next(arg for arg in args if arg.startswith("--ddl="))
    for statement in ddl.removeprefix("--ddl=").split(";"):
        statement = " ".join(statement.split())
        view = re.match(r"CREATE VIEW (\\w+)", statement)
        role = re.match(r"CREATE ROLE (\\w+)", statement)
        if view:
            assert view[1] not in state["views"], "duplicate view"
            state["views"].append(view[1])
        elif role:
            assert role[1] not in state["roles"], "duplicate role"
            state["roles"].append(role[1])
        else:
            assert statement.startswith("GRANT "), statement
            state["grants"].append(statement)
    state_file.write_text(json.dumps(state))
'''


def _migrate(tmp_path: Path, *, fail_check: str = "") -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "gcloud"
    stub.write_text("#!" + sys.executable + "\n" + _FAKE_GCLOUD)
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:/usr/bin:/bin",
        "FAKE_STATE": str(tmp_path / "state.json"),
        "FAIL_CHECK": fail_check,
        "GCP_PROJECT_ID": "test-project",
        "SPANNER_INSTANCE_ID": "test-instance",
        "SPANNER_DATABASE_ID": "test-database",
    }
    return subprocess.run(  # noqa: S603 - fixed repository script, isolated fake CLI
        ["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, check=False,
    )


def _state(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "state.json").read_text())


def test_the_migration_adds_the_view_then_the_role_once_and_is_idempotent(
    tmp_path: Path,
) -> None:
    first = _migrate(tmp_path)
    assert first.returncode == 0, first.stderr
    assert first.stdout.split("\n")[:2] == [f"created {VIEW}", f"created {ROLE}"]
    second = _migrate(tmp_path)
    assert second.returncode == 0, second.stderr
    assert second.stdout.split("\n")[:2] == [f"{VIEW} exists", f"{ROLE} exists"]

    state = _state(tmp_path)
    assert state["views"] == [VIEW]
    assert state["roles"] == [ROLE]
    # view check, view apply, role check, role apply; then the two checks alone.
    assert [("ddl" in call) for call in state["calls"]] == [
        False, True, False, True, False, False,
    ]
    for call in state["calls"]:
        assert "test-database" in call
        assert "--project=test-project" in call
        assert "--instance=test-instance" in call


def test_the_role_reaches_exactly_the_objects_the_node_programs_use(tmp_path: Path) -> None:
    assert _migrate(tmp_path).returncode == 0
    grants = _state(tmp_path)["grants"]
    assert grants == [
        f"GRANT SELECT, DELETE ON TABLE tr_analytics_outbox, tr_operational_analytics_outbox "
        f"TO ROLE {ROLE}",
        f"GRANT SELECT, INSERT, UPDATE ON TABLE tr_operational_outbox_heartbeat TO ROLE {ROLE}",
        f"GRANT SELECT ON TABLE tr_generation, tr_reservation TO ROLE {ROLE}",
        f"GRANT SELECT ON VIEW {VIEW} TO ROLE {ROLE}",
    ]
    assert not any("tr_entities" in grant for grant in grants)


def test_the_view_exposes_only_the_directory_fields_of_workspace_rows(tmp_path: Path) -> None:
    assert _migrate(tmp_path).returncode == 0
    ddl = next(arg for arg in _state(tmp_path)["calls"][1] if arg.startswith("--ddl="))
    statement = " ".join(ddl.removeprefix("--ddl=").split())
    assert statement.startswith(f"CREATE VIEW {VIEW} SQL SECURITY DEFINER AS SELECT e.id AS id,")
    assert statement.endswith("FROM tr_entities AS e WHERE e.kind = 'workspace'")
    paths = re.findall(r"JSON_QUERY\(e\.body, '\$\.(\w+)'\) AS (\w+)", statement)
    assert paths == [
        ("id", "workspace_id"),
        ("name", "name"),
        ("deleted", "deleted"),
        ("owner_user_id", "owner_user_id"),
        ("federated_home", "federated_home"),
        ("created_at", "created_at"),
    ]
    assert "e.body AS" not in statement


def test_a_view_check_that_fails_stops_the_migration_without_applying(tmp_path: Path) -> None:
    """An unreadable schema is not an absent view: no blind apply."""
    result = _migrate(tmp_path, fail_check="views")
    assert result.returncode == 23
    state = _state(tmp_path)
    assert len(state["calls"]) == 1
    assert state["views"] == [] and state["roles"] == []


def test_a_role_check_that_fails_stops_the_migration_before_the_grants(tmp_path: Path) -> None:
    result = _migrate(tmp_path, fail_check="roles")
    assert result.returncode == 24
    state = _state(tmp_path)
    assert state["views"] == [VIEW]
    assert state["roles"] == [] and state["grants"] == []


def test_production_rollout_applies_it_after_every_table_it_grants_on() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy-production.yml").read_text())
    steps = workflow["jobs"]["migrate-schema"]["steps"]
    runs = [step.get("run") for step in steps]
    added = runs.index("scripts/deploy/migrate_clickhouse_node_role.sh")
    for prerequisite in (
        "scripts/deploy/migrate_generation_records.sh --apply",
        "scripts/deploy/migrate_analytics_outbox.sh",
        "scripts/deploy/migrate_operational_outbox_heartbeat.sh",
        "scripts/deploy/migrate_operational_analytics_outbox.sh",
    ):
        assert runs.index(prerequisite) < added, prerequisite
    sibling = runs.index("scripts/deploy/migrate_operational_analytics_outbox.sh")
    for field in ("env", "if", "shell", "continue-on-error"):
        assert steps[added].get(field) == steps[sibling].get(field)


def test_every_deploy_entry_point_applies_it_before_the_node_code_ships() -> None:
    script = (ROOT / "scripts/deploy-gcp.sh").read_text()
    assert script.index("migrate_operational_outbox_heartbeat.sh") < script.index(
        "migrate_clickhouse_node_role.sh"
    )
    for path in (
        "scripts/deploy/clickhouse_operational_analytics.sh",
        "scripts/deploy/clickhouse_live_ingestion.sh",
    ):
        node_deploy = (ROOT / path).read_text()
        role = node_deploy.index("migrate_clickhouse_node_role.sh")
        assert node_deploy.index("migrate_operational_outbox_heartbeat.sh") < role, path
        assert role < node_deploy.index('build_clickhouse_bundle "$ROOT" "$archive"'), path


def test_the_node_account_may_sign_in_as_this_role_and_no_other() -> None:
    terraform = (ROOT / "infra/clickhouse_node_role.tf").read_text()
    assert re.search(r'clickhouse_node_role\s*=\s*"' + ROLE + '"', terraform)
    assert 'role     = "roles/spanner.fineGrainedAccessUser"' in terraform
    assert 'role     = "roles/spanner.databaseRoleUser"' in terraform
    assert (
        'resource.name.endsWith(\\"/databaseRoles/${local.clickhouse_node_role}\\")'
        in terraform
    )
    # Database-wide access is not granted here.
    assert "roles/spanner.databaseUser" not in terraform.replace(
        "roles/spanner.databaseUser binding", ""
    )
