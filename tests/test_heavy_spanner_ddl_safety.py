from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASH = Path("/bin/bash")
MIGRATION = ROOT / "scripts" / "deploy" / "migrate_entity_ttl.sh"
DEPLOY_WORKFLOW = ROOT / ".github" / "workflows" / "deploy.yml"


def _fake_gcloud(tmp_path: Path) -> tuple[dict[str, str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls"
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(
        textwrap.dedent(
            """\
            #!/bin/sh
            printf '%s\\n' "$*" >> "$TR_TEST_GCLOUD_CALLS"
            case "$*" in
              *INFORMATION_SCHEMA.COLUMNS*) printf '0\\n' ;;
              *INFORMATION_SCHEMA.TABLES*) printf '\\n' ;;
              *'operations list'*) printf '\\n' ;;
              *) printf '\\n' ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    gcloud.chmod(0o755)
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "TR_TEST_GCLOUD_CALLS": str(calls),
            "GCP_PROJECT_ID": "test-project",
            "SPANNER_INSTANCE_ID": "test-instance",
            "SPANNER_DATABASE_ID": "test-database",
        }
    )
    return env, calls


def test_heavy_entity_ttl_migration_is_not_part_of_routine_deploy() -> None:
    workflow = DEPLOY_WORKFLOW.read_text(encoding="utf-8")

    assert "migrate_entity_ttl.sh" not in workflow


def test_entity_ttl_migration_defaults_to_verification_only(tmp_path: Path) -> None:
    env, calls = _fake_gcloud(tmp_path)

    result = subprocess.run(  # noqa: S603 - fixed repository script under test
        [str(BASH), str(MIGRATION)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "verification mode will not run heavy DDL" in result.stdout
    assert "ddl update" not in calls.read_text(encoding="utf-8")


def test_entity_ttl_migration_requires_specific_acknowledgement(tmp_path: Path) -> None:
    env, calls = _fake_gcloud(tmp_path)

    result = subprocess.run(  # noqa: S603 - fixed repository script under test
        [str(BASH), str(MIGRATION), "--apply"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert "migration-specific acknowledgement" in result.stdout
    assert "ddl update" not in calls.read_text(encoding="utf-8")


def test_entity_ttl_migration_applies_only_after_both_gates(tmp_path: Path) -> None:
    env, calls = _fake_gcloud(tmp_path)
    env["TR_HEAVY_DDL_ACK"] = "tr_entities.ephemeral_expires_at"

    result = subprocess.run(  # noqa: S603 - fixed repository script under test
        [str(BASH), str(MIGRATION), "--apply"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    recorded = calls.read_text(encoding="utf-8")
    assert recorded.count("ddl update") == 2
    assert recorded.count("operations list") == 2
