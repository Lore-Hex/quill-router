"""The switch's writes, fastpath/testdata/switch_statements_v1.json
(docs/design/fast-admission-widening.md, sections 5.5 and 5.6): the fixture
Go's Store.SetWorkspace executes and the enabler (PR 6) reads, held here to
the production schema the migrations create, so the columns it writes exist
as the enabler will write them."""

from __future__ import annotations

import json
import re
from pathlib import Path

from tests.conformance import spanner_ddl

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "fastpath" / "testdata" / "switch_statements_v1.json"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text())


def _columns(table: str) -> set[str]:
    statement = next(s for s in spanner_ddl.DDL if s.startswith(f"CREATE TABLE {table} ("))
    body = statement[len(f"CREATE TABLE {table} (") : statement.rindex(") PRIMARY KEY")]
    return {m.group(1) for m in re.finditer(r"(?:^|,)\s*(\w+) (?:STRING|BOOL|TIMESTAMP|INT64|BYTES)", body)}


def test_the_fixture_has_its_three_operations_and_version_one() -> None:
    fixture = _fixture()
    assert fixture["version"] == 1
    assert set(fixture["operations"]) == {"enable", "disable", "disable_unheld"}


def test_every_mutation_writes_columns_the_production_schema_has() -> None:
    for name, op in _fixture()["operations"].items():
        for mutation in op["mutations"]:
            assert mutation["op"] == "insert_or_update", name
            assert len(mutation["columns"]) == len(mutation["values"]), name
            assert set(mutation["columns"]) <= _columns(mutation["table"]), (name, mutation["table"])


def test_the_unheld_disable_is_the_disable_less_its_hold() -> None:
    ops = _fixture()["operations"]
    disable, unheld = ops["disable"], ops["disable_unheld"]
    holds = [m for m in disable["mutations"] if m["table"] == "tr_fastpath_workspace_hold"]
    assert len(holds) == 1 and holds[0]["values"][holds[0]["columns"].index("reason")] == "@reason"
    assert [m for m in disable["mutations"] if m["table"] != "tr_fastpath_workspace_hold"] == unheld["mutations"]
    assert disable["statements"] == unheld["statements"]
    assert all("tr_fastpath_workspace_hold" not in s["sql"] for s in unheld["statements"])
    assert all(m["table"] != "tr_fastpath_workspace_hold" for m in unheld["mutations"])


def test_enable_removes_the_hold_and_every_disable_revokes() -> None:
    ops = _fixture()["operations"]
    assert [s["sql"] for s in ops["enable"]["statements"]] == [
        "DELETE FROM tr_fastpath_workspace_hold WHERE workspace_id = @workspace"
    ]
    for name in ("disable", "disable_unheld"):
        assert [s["counts"] for s in ops[name]["statements"]] == ["revoked_leases"]
        assert ops[name]["statements"][0]["sql"].startswith("UPDATE tr_lease SET revoked = TRUE")


def test_the_runbook_names_the_lines_the_migration_prints_for_the_hold_tables() -> None:
    """A release with holds is pinned only once its tables are seen created
    in a control-plane deploy's migration step: the runbook, the design
    note and the watch's fallback must name what the migration prints."""
    migration = (ROOT / "scripts" / "deploy" / "migrate_fastpath.sh").read_text()
    deploy = (ROOT / "docs" / "runbooks" / "fastpath-deploy.md").read_text()
    design = (ROOT / "docs" / "design" / "fast-admission-widening.md").read_text()
    pinned = deploy.split("## Taking a pinned release", 1)[1].split("\n## ", 1)[0]
    for table in ("tr_fastpath_workspace_hold", "tr_fastpath_kill"):
        for outcome in ("created", "already present"):
            line = f"{table}: {outcome}"
            assert f'log "{line}"' in migration, line
            assert f"`{line}`" in pinned, line
        assert f"`{table}`" in design.split("4. **Holds.**", 1)[1].split("5. **Python", 1)[0], table
    turn_off = (ROOT / "docs" / "runbooks" / "fastpath-turn-off.md").read_text()
    assert "### When disable fails" in turn_off
    watch = (ROOT / "fastpath" / "cmd" / "fastpathwatch" / "main.go").read_text()
    assert "fastpath-turn-off.md" in watch and "TR_FASTPATH_PILOT_ENABLED=false" in watch


def test_the_hold_reasons_a_stop_writes_are_the_schemas() -> None:
    statement = next(s for s in spanner_ddl.DDL if s.startswith("CREATE TABLE tr_fastpath_workspace_hold ("))
    reasons = re.search(r"reason IN \(([^)]*)\)", statement)
    assert reasons is not None
    allowed = {r.strip().strip("'") for r in reasons.group(1).split(",")}
    assert {"operator", "watch", "backfill", "paused"} <= allowed
