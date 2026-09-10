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


def test_spanner_migration_existing_read_write_index_is_a_noop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                (r"INFORMATION_SCHEMA\.(COLUMNS|INDEXES)", "1"),
            ),
        ),
    )

    run = DeployScriptHarness(tmp_path).run(SCRIPT)

    assert run.returncode == 0, run.stderr
    assert _ddls(run) == []
    assert not any(call[0] == "sleep" for call in run.calls)


@pytest.mark.parametrize("state", ["CREATING", "WRITE_ONLY"])
def test_spanner_migration_times_out_waiting_for_unready_existing_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    state: str,
) -> None:
    monkeypatch.setitem(
        SCRIPT_FIXTURES,
        SCRIPT,
        ScriptFixture(
            env={
                "GCP_PROJECT_ID": "test-project",
                "SPANNER_INSTANCE_ID": "test-instance",
                "SPANNER_DATABASE_ID": "test-database",
                "RECEIPT_KEY_INDEX_WAIT_ATTEMPTS": "3",
                "RECEIPT_KEY_INDEX_WAIT_SECONDS": "0",
            },
            responses=(
                (r"INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES", state),
                (r"INFORMATION_SCHEMA\.(COLUMNS|INDEXES)", "1"),
            ),
        ),
    )

    run = DeployScriptHarness(tmp_path).run(SCRIPT)

    assert run.returncode != 0
    assert _ddls(run) == []
    assert sum(call[0] == "sleep" for call in run.calls) == 3
    assert (
        "ERROR: timed out waiting for tr_receipt_key_versions to become READ_WRITE "
        f"after 3 attempts (last state={state})"
    ) in run.stdout


def test_postgres_runtime_schema_migrates_nullable_receipt_version_index() -> None:
    schema = (ROOT / "src/trusted_router/storage_postgres_schema.sql").read_text()

    assert "ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS kid TEXT;" in schema
    assert "ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS att_sha256 TEXT;" in schema
    index = (
        "CREATE INDEX IF NOT EXISTS tr_receipt_key_versions\n"
        "    ON tr_entities (kid, att_sha256)\n"
        "    WHERE kid IS NOT NULL AND att_sha256 IS NOT NULL;"
    )
    assert index in schema
    assert schema.index("ALTER TABLE tr_entities ADD COLUMN IF NOT EXISTS att_sha256 TEXT;") < (
        schema.index(index)
    )
    assert "kid TEXT NOT NULL" not in schema
    assert "att_sha256 TEXT NOT NULL" not in schema


def test_deploy_applies_receipt_version_schema_before_router_rollout() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    migrate = workflow.index("scripts/deploy/migrate_receipt_key_versions.sh")
    deploy = workflow.index("\n  deploy:\n")

    assert migrate < deploy


def test_receipt_docs_require_ddl_before_the_new_writer() -> None:
    docs = (ROOT / "docs/client-receipts.md").read_text()

    assert "Apply that DDL before\nstarting this router revision" in docs
    assert "writer names the physical `kid`\nand `att_sha256` columns" in docs
    assert "safe to run\nbefore or after the compatible router" not in docs


def test_public_receipt_docs_match_listing_and_version_lookup_contracts() -> None:
    docs = (ROOT / "docs/client-receipts.md").read_text()
    page = (ROOT / "src/trusted_router/templates/public/receipts.html").read_text()

    assert "newest observed attestation version for each `kid`" in docs
    assert "pages of at most 250 keys\nand at most 1 MiB" in docs
    assert "every retained attestation version for exactly one\nsigning key" in docs
    assert "newest observed attestation version for each <code>kid</code>" in page
    assert "pages of at most 250 keys and capped at 1 MiB per response" in page
    assert "Every retained attestation version for exactly one signing key" in page
    assert "every observed attestation re-mint for every signing key" not in page
