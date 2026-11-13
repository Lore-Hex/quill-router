"""The ClickHouse node units connect to Spanner as tr_clickhouse_node (step 3)."""

from __future__ import annotations

import re
from pathlib import Path

from clickhouse._spanner_role import DATABASE_ROLE_ENV, database_role

ROOT = Path(__file__).resolve().parents[1]
UNITS = ROOT / "clickhouse"
ROLE = "tr_clickhouse_node"
LIVE_DEPLOY = ROOT / "scripts/deploy/clickhouse_live_ingestion.sh"


def _environment(unit: Path) -> dict[str, str]:
    lines = re.findall(r"^Environment=(\w+)=(.*)$", unit.read_text(), re.M)
    return dict(lines)


def _programs(unit: Path) -> list[str]:
    return re.findall(r"^ExecStart=\S+ -m clickhouse\.(\w+)", unit.read_text(), re.M)


def _uses_spanner(program: str) -> bool:
    """Opens Spanner through the client library or the REST API, by any route."""
    source = (UNITS / f"{program}.py").read_text()
    return any(
        marker in source
        for marker in ("google.cloud import spanner", "google.cloud.spanner", "spanner.googleapis.com")
    )


def test_every_program_that_opens_spanner_honours_the_role_variable() -> None:
    for path in UNITS.glob("*.py"):
        if path.name != "_spanner_role.py" and _uses_spanner(path.stem):
            assert "clickhouse._spanner_role" in path.read_text(), path.name


def test_every_unit_whose_program_opens_spanner_uses_the_role_and_no_other_does() -> None:
    with_role = set()
    for unit in sorted(UNITS.glob("tr-clickhouse-*.service")):
        environment = _environment(unit)
        if any(_uses_spanner(program) for program in _programs(unit)):
            assert database_role(environment) == ROLE, unit.name
            with_role.add(unit.name)
        else:
            assert DATABASE_ROLE_ENV not in environment, unit.name
    assert with_role == {
        "tr-clickhouse-ingest.service",
        "tr-clickhouse-operational-ingest.service",
        "tr-clickhouse-overrun-rollup.service",
        "tr-clickhouse-spanner-delivery.service",
        "tr-clickhouse-workspace-directory.service",
    }


def test_the_units_name_the_role_the_migration_creates() -> None:
    migration = (ROOT / "scripts/deploy/migrate_clickhouse_node_role.sh").read_text()
    assert f'--ddl="CREATE ROLE {ROLE}"' in migration


def test_no_writer_of_the_shared_environment_file_can_override_the_role() -> None:
    """systemd lets EnvironmentFile= override Environment=, and a blank value
    turns the role off, so every writer of the file must write the password alone."""
    def writes_it(text: str) -> bool:
        # Directly, or through ENV_FILE (the node-side scripts' name for it).
        return bool(
            re.search(r"cat > /etc/tr-clickhouse-ingest\.env\b", text)
            or (
                re.search(r'^ENV_FILE="[^"]*/etc/tr-clickhouse-ingest\.env"', text, re.M)
                and re.search(r'(?:replace_file|cat >|tee|>) "\$ENV_FILE"', text)
            )
        )

    writers = [path for path in (ROOT / "scripts").rglob("*.sh") if writes_it(path.read_text())]
    assert {path.name for path in writers} == {
        "clickhouse_live_ingestion.sh", "clickhouse_cluster.sh", "rotate_tr_password.sh",
    }
    for path in writers:
        text = path.read_text()
        assert DATABASE_ROLE_ENV not in text, path.name
        # The only line any of them prints into the file is the password.
        printed = re.findall(r"printf '([A-Z_]+)=%s\\n'", text)
        assert set(printed) == {"CH_PASSWORD"}, path.name


def test_the_live_deploy_refreshes_every_role_unit_before_reloading_and_restarting() -> None:
    script = LIVE_DEPLOY.read_text()
    reload = script.index("systemctl daemon-reload")
    refresh = re.search(
        r'for unit in ([^;]+); do\n\s+if \[ -f \\"/etc/systemd/system/\\\$unit\\" \]; then\n'
        r'\s+install -m 0644 \\"/opt/tr-clickhouse/clickhouse/\\\$unit\\"',
        script,
    )
    assert refresh, "the guarded refresh of the operational units is missing"
    refreshed = set(refresh[1].split())
    installed = set(re.findall(r"clickhouse/(tr-clickhouse-[\w-]+\.service) \\\n", script))
    for unit in UNITS.glob("tr-clickhouse-*.service"):
        if DATABASE_ROLE_ENV in unit.read_text():
            assert unit.name in installed | refreshed, unit.name
    assert refresh.start() < reload
    restart = script.index("for unit in tr-clickhouse-operational-ingest.service tr-clickhouse-operational-ingest-postgres.service")
    assert reload < restart
