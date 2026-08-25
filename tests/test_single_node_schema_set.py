"""The standalone-node schema set, and the scripts that apply it.

Three scripts each carried their own copy of "which migrations does a standalone
ClickHouse node need": two literal `006`+`009` lists and one `clickhouse/00*.sql`
glob. All three were wrong the same way -- 010 through 013 landed and none of
them picked those up -- and nothing failed, because the list lived in a shell
glob and a NEXT_STEPS heredoc where no test could see it.

The consequence is specifically silent. clickhouse/013_*.sql adds the
workspace_id column the drain inserts; an un-migrated node REJECTS the insert;
shard failures are contained, so the unit stays active and reports healthy while
delivering nothing.

These run the derivation rather than reading it, so a script that stops using it
fails here instead of at 3am on a node nobody is watching.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/deploy/_clickhouse_single_node_schema.sh"
PARIS = ROOT / "scripts/deploy/aws_eu_clickhouse.sh"
STOCKHOLM = ROOT / "scripts/deploy/aws_eu_north_clickhouse.sh"


def _derive(root: Path) -> subprocess.CompletedProcess[str]:
    argv = ["bash", "-c", f'. "{HELPER}"; single_node_migrations "$1"', "_", str(root)]  # noqa: S607 - bash from PATH, as CI runs it
    return subprocess.run(  # noqa: S603 - fixed argv; running the real helper is the point
        argv,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_set_is_derived_from_the_directory() -> None:
    """Equality against what is actually on disk, NOT a literal list.

    A literal here would rot in exactly the way the thing it is testing rotted.
    """
    expected = sorted(p.name for p in (ROOT / "clickhouse").glob("*_single_node.sql"))
    result = _derive(ROOT)

    assert result.returncode == 0, result.stderr
    derived = [Path(line).name for line in result.stdout.split()]
    assert derived == expected
    # and the set is not the historical pair that started this
    assert len(derived) > 2, "if this ever drops back to 006+009, something reverted"


def test_the_workspace_id_migration_is_in_the_set() -> None:
    """Named explicitly because it is the one with teeth: its own header says it
    MUST be applied before the clickhouse/ tree is next shipped to a node."""
    result = _derive(ROOT)

    assert result.returncode == 0, result.stderr
    names = {Path(line).name for line in result.stdout.split()}
    assert "013_activity_generations_workspace_id_single_node.sql" in names
    assert "011_workspace_directory_single_node.sql" in names


def test_every_single_node_migration_targets_the_default_database() -> None:
    """Standalone nodes connect to `default`: the AWS control plane and drain
    installer both default CH_DATABASE to it, and so does Azure. Only GCP uses
    `tr`, via the ON CLUSTER migrations, which are not in this set. A qualified
    statement here would apply somewhere nothing reads -- and SUCCEED, which is
    worse than being skipped."""
    result = _derive(ROOT)

    assert result.returncode == 0, result.stderr


def test_a_qualified_migration_is_refused(tmp_path: Path) -> None:
    """The guard above, shown failing. 011 and 013 really did ship qualified."""
    (tmp_path / "clickhouse").mkdir()
    (tmp_path / "clickhouse" / "900_ok_single_node.sql").write_text(
        "CREATE TABLE IF NOT EXISTS fine (x Int64) ENGINE = MergeTree ORDER BY x;\n"
    )
    (tmp_path / "clickhouse" / "901_bad_single_node.sql").write_text(
        "ALTER TABLE tr.activity_generations ADD COLUMN IF NOT EXISTS y Int64;\n"
    )

    result = _derive(tmp_path)

    assert result.returncode != 0
    assert "901_bad_single_node.sql" in result.stderr
    assert "default" in result.stderr


def test_an_empty_set_is_refused_rather_than_applied(tmp_path: Path) -> None:
    """A rename would otherwise apply nothing and look like success."""
    (tmp_path / "clickhouse").mkdir()
    (tmp_path / "clickhouse" / "006_renamed_away.sql").write_text("SELECT 1;\n")

    result = _derive(tmp_path)

    assert result.returncode != 0
    assert "EMPTY schema" in result.stderr


@pytest.mark.parametrize("script", [PARIS, STOCKHOLM], ids=["paris", "stockholm"])
def test_node_scripts_use_the_shared_derivation(script: Path) -> None:
    text = script.read_text(encoding="utf-8")

    assert "_clickhouse_single_node_schema.sh" in text
    assert "single_node_migrations" in text
    # The two shapes this replaced, neither of which may come back.
    assert "clickhouse/00*.sql" not in text
    assert "006_operational_analytics_single_node.sql" not in text
    assert "009_client_events_single_node.sql" not in text


def test_paris_applies_the_schema_instead_of_printing_it() -> None:
    """It used to be step 1 of a NEXT_STEPS heredoc, described as a human step
    needing the ClickHouse password -- which the script had already read from
    Secrets Manager to write users.d. The only thing the human step added was a
    chance to run a stale command, and it was stale."""
    text = PARIS.read_text(encoding="utf-8")

    assert "--multiquery < /root/operational_schema.sql" in text
    assert "OPERATIONAL_SCHEMA" in text
    # the instruction is gone from the operator steps
    next_steps = text.split("NEXT_STEPS=$(cat <<NEXT", 1)[1]
    assert "apply the schema" not in next_steps
    assert "clickhouse-client" not in next_steps


def test_both_nodes_of_the_aws_cloud_apply_the_same_set() -> None:
    """Stockholm exists to be a second copy of Paris's history. Two nodes built
    from different schema sets are not copies of each other."""
    paris = PARIS.read_text(encoding="utf-8")
    stockholm = STOCKHOLM.read_text(encoding="utf-8")

    for text in (paris, stockholm):
        assert 'single_node_migrations "' in text
        assert 'OPERATIONAL_SCHEMA="$(cat "${SCHEMA_FILES[@]}")"' in text
