from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.deploy_script_harness import SCRIPT_FIXTURES, DeployScriptHarness, ScriptFixture
from tests.test_spend_lease_migration import _ddls

ROOT = Path(__file__).parents[1]
SCRIPT = "scripts/deploy/migrate_receipt_key_versions.sh"


def test_spanner_receipt_key_version_migration_is_additive_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = ROOT / SCRIPT
    assert os.access(path, os.X_OK)
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        ScriptFixture(
            env={
                "GCP_PROJECT_ID": "test-project",
                "SPANNER_INSTANCE_ID": "test-instance",
                "SPANNER_DATABASE_ID": "test-database",
            },
            responses=(
                (r"INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES", "READ_WRITE"),
                (r"INFORMATION_SCHEMA\.(COLUMNS|INDEXES)", "0"),
            ),
        ),
    )

    run = DeployScriptHarness(tmp_path).run(SCRIPT)

    assert run.returncode == 0, run.stderr
    assert _ddls(run) == [
        "ALTER TABLE tr_entities ADD COLUMN kid STRING(43)",
        "ALTER TABLE tr_entities ADD COLUMN att_sha256 STRING(43)",
        "CREATE NULL_FILTERED INDEX tr_receipt_key_versions "
        "ON tr_entities (kid, att_sha256)",
    ]


def test_postgres_schema_has_nullable_receipt_version_projection_and_index() -> None:
    schema = (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()

    assert "ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS kid TEXT;" in schema
    assert "ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS att_sha256 TEXT;" in schema
    assert "ON tr_entities (kid, att_sha256);" in schema
    assert "kid TEXT NOT NULL" not in schema
    assert "att_sha256 TEXT NOT NULL" not in schema


def test_deploy_applies_receipt_version_schema_before_router_rollout() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    migrate = workflow.index("scripts/deploy/migrate_receipt_key_versions.sh")
    deploy = workflow.index("\n  deploy:\n")

    assert migrate < deploy
