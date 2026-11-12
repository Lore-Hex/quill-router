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
    sql = " ".join(next(arg for arg in args if arg.startswith("--sql=")).split())
    if "INFORMATION_SCHEMA.VIEWS" in sql:
        if fail == "views":
            sys.exit(23)
        print(int(re.search(r"table_name='([^']+)'", sql)[1] in state["views"]))
    elif "INFORMATION_SCHEMA.ROLES" in sql:
        if fail == "roles":
            sys.exit(24)
        print(int(re.search(r"role_name='([^']+)'", sql)[1] in state["roles"]))
    else:
        assert "INFORMATION_SCHEMA.ROLE_TABLE_GRANTS" in sql, sql
        assert "grantee='tr_clickhouse_node'" in sql and "table_schema=''" in sql, sql
        if fail == "grants":
            sys.exit(25)
        key = [re.search(r"table_name='([^']+)'", sql)[1],
               re.search(r"privilege_type='([^']+)'", sql)[1]]
        print(int(key in state["grants"]))
else:
    ddl = next(arg for arg in args if arg.startswith("--ddl="))
    assert ";" not in ddl, "one statement per schema change"
    statement = " ".join(ddl.removeprefix("--ddl=").split())
    view = re.match(r"CREATE VIEW (\\w+)", statement)
    role = re.fullmatch(r"CREATE ROLE (\\w+)", statement)
    grant = re.fullmatch(r"GRANT (\\w+) ON (?:TABLE|VIEW) (\\w+) TO ROLE tr_clickhouse_node", statement)
    if view:
        assert view[1] not in state["views"], "duplicate view"
        state["views"].append(view[1])
    elif role:
        assert role[1] not in state["roles"], "duplicate role"
        state["roles"].append(role[1])
    else:
        assert grant, statement
        key = [grant[2], grant[1]]
        assert key not in state["grants"], "duplicate privilege"
        state["grants"].append(key)
        state.setdefault("statements", []).append(statement)
    state_file.write_text(json.dumps(state))
'''

EXPECTED = [
    ["tr_analytics_outbox", "SELECT"],
    ["tr_analytics_outbox", "DELETE"],
    ["tr_operational_analytics_outbox", "SELECT"],
    ["tr_operational_analytics_outbox", "DELETE"],
    ["tr_operational_outbox_heartbeat", "SELECT"],
    ["tr_operational_outbox_heartbeat", "INSERT"],
    ["tr_operational_outbox_heartbeat", "UPDATE"],
    ["tr_generation", "SELECT"],
    ["tr_reservation", "SELECT"],
    [VIEW, "SELECT"],
]


def _migrate(
    tmp_path: Path, *, fail_check: str = "", seed: dict | None = None,
) -> subprocess.CompletedProcess[str]:
    if seed is not None:
        (tmp_path / "state.json").write_text(json.dumps(
            {"calls": [], "views": [], "roles": [], "grants": [], **seed}))
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


def test_the_migration_adds_the_view_the_role_and_each_privilege_once(
    tmp_path: Path,
) -> None:
    first = _migrate(tmp_path)
    assert first.returncode == 0, first.stderr
    assert first.stdout.split("\n")[:2] == [f"created {VIEW}", f"created {ROLE}"]
    second = _migrate(tmp_path)
    assert second.returncode == 0, second.stderr
    assert second.stdout.strip().split("\n") == [f"{VIEW} exists", f"{ROLE} exists"]

    state = _state(tmp_path)
    assert state["views"] == [VIEW]
    assert state["roles"] == [ROLE]
    assert state["grants"] == EXPECTED
    applied = [call for call in state["calls"] if "ddl" in call]
    assert len(applied) == 2 + len(EXPECTED)
    # The second run only reads: view, role and one check per privilege.
    second_calls = state["calls"][len(state["calls"]) - (2 + len(EXPECTED)):]
    assert not any("ddl" in call for call in second_calls)
    for call in state["calls"]:
        assert "test-database" in call
        assert "--project=test-project" in call
        assert "--instance=test-instance" in call


def test_a_run_that_stopped_partway_is_completed_by_the_next(tmp_path: Path) -> None:
    """A role whose privileges were only partly applied gets the rest, nothing twice."""
    result = _migrate(
        tmp_path, seed={"views": [VIEW], "roles": [ROLE], "grants": EXPECTED[:3]},
    )
    assert result.returncode == 0, result.stderr
    state = _state(tmp_path)
    assert state["grants"] == EXPECTED
    assert state["statements"] == [
        f"GRANT {privilege} ON {'VIEW' if table == VIEW else 'TABLE'} {table} TO ROLE {ROLE}"
        for table, privilege in EXPECTED[3:]
    ]


def test_the_role_reaches_exactly_the_objects_the_node_programs_use(tmp_path: Path) -> None:
    assert _migrate(tmp_path).returncode == 0
    granted = {table for table, _ in _state(tmp_path)["grants"]}
    assert "tr_entities" not in granted
    # Every Spanner object the five node programs name, besides the legacy
    # heartbeat row they skip under a role, must be granted, and nothing else.
    used: set[str] = set()
    for program in (
        "ingest_outbox", "ingest_operational_outbox", "refresh_workspace_directory",
        "verify_spanner_delivery", "rollup_reservation_overruns",
    ):
        source = (ROOT / f"clickhouse/{program}.py").read_text()
        used |= set(re.findall(r"(?:FROM|INTO|JOIN)\s+(tr_\w+)", source))
        # A statement split across string literals: "... FROM " "tr_x@{FORCE_INDEX=...}".
        used |= set(re.findall(r'"(tr_\w+)@\{FORCE_INDEX', source))
        used |= set(re.findall(r'^[A-Z_]*TABLE = "(tr_\w+)"', source, re.M))
        used |= set(re.findall(r'^SOURCE_VIEW = "(tr_\w+)"', source, re.M))
    used.discard("tr_entities")
    assert used == granted


def test_the_view_exposes_only_the_directory_fields_of_workspace_rows(tmp_path: Path) -> None:
    assert _migrate(tmp_path).returncode == 0
    ddl = next(
        arg for call in _state(tmp_path)["calls"] for arg in call
        if arg.startswith("--ddl=CREATE VIEW")
    )
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


def test_a_privilege_check_that_fails_stops_without_applying_it(tmp_path: Path) -> None:
    result = _migrate(tmp_path, fail_check="grants")
    assert result.returncode == 25
    state = _state(tmp_path)
    assert state["roles"] == [ROLE]
    assert state["grants"] == []


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
