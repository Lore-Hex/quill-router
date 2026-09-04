from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from .deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    HarnessRun,
    ScriptFixture,
    summarise,
)

ROOT = Path(__file__).parents[1]
SCRIPT = "scripts/deploy/migrate_spend_lease.sh"

AUTHORIZATION_COLUMNS = {
    "spend_lease_id": "STRING(64)",
    "spend_lease_gen": "INT64",
    "spend_lease_allocated_micro": "INT64",
    "spend_lease_token": "STRING(MAX)",
    "spend_lease_status": "STRING(16)",
    "spend_lease_exp": "TIMESTAMP",
    "idempotency_fingerprint": "STRING(64)",
    "finalization_outcome": "STRING(32)",
    "finalized_cost_microdollars": "INT64",
    "spend_lease_admission_receipt": "STRING(MAX)",
    "spend_lease_receipt_hash": "STRING(64)",
    "started_at": "TIMESTAMP",
    "heartbeat_seq": "INT64",
    "heartbeat_at": "TIMESTAMP",
    "heartbeat_hash": "STRING(64)",
    "selected_endpoint_id": "STRING(128)",
    "delivered_usage": "STRING(MAX)",
    "pricing_snapshot": "STRING(MAX)",
}
STAGE_C_NULLABLE_AUTHORIZATION_COLUMNS = (
    "spend_lease_id",
    "spend_lease_gen",
    "spend_lease_allocated_micro",
    "spend_lease_token",
    "spend_lease_status",
    "spend_lease_exp",
    "idempotency_fingerprint",
    "finalization_outcome",
    "finalized_cost_microdollars",
    "spend_lease_admission_receipt",
    "spend_lease_receipt_hash",
)

ARBITRATION_COLUMNS = {
    "scope_salt": "STRING(4) NOT NULL",
    "idempotency_scope": "STRING(256) NOT NULL",
    "registration_kind": "STRING(16) NOT NULL",
    "authorization_id": "STRING(64)",
    "spend_lease_id": "STRING(64)",
    "spend_lease_gen": "INT64",
    "spend_lease_allocated_micro": "INT64",
    "provisional_id": "STRING(64)",
    "created_at": "TIMESTAMP NOT NULL",
    "terminal_at": "TIMESTAMP",
}

OPEN_COLUMNS = {
    "lease_id": "STRING(64) NOT NULL",
    "phase": "STRING(16) NOT NULL",
    "gen": "INT64 NOT NULL",
    "key_hash": "STRING(64) NOT NULL",
    "boot_kid": "STRING(64) NOT NULL",
    "cap_micro": "INT64 NOT NULL",
    "skew_seconds": "INT64 NOT NULL",
    "workspace_id": "STRING(64) NOT NULL",
    "region": "STRING(32) NOT NULL",
    "creating_authorization_id": "STRING(64) NOT NULL",
    "idempotency_scope": "STRING(256) NOT NULL",
    "expires_at": "TIMESTAMP NOT NULL",
    "next_attempt_at": "TIMESTAMP",
    "attempts": "INT64 NOT NULL DEFAULT (0)",
    "last_error": "STRING(MAX)",
    "dead": "BOOL NOT NULL DEFAULT (false)",
    "close_eligible_since": "TIMESTAMP",
    "global_closed_at": "TIMESTAMP",
    "local_closed_at": "TIMESTAMP",
    "recovering_at": "TIMESTAMP OPTIONS (allow_commit_timestamp = true)",
    "created_at": "TIMESTAMP NOT NULL",
}

ARBITRATION_SHAPE_CHECK = (
    "CONSTRAINT spend_lease_scope_arbitration_shape CHECK ((registration_kind = 'BOUND' "
    "AND authorization_id IS NOT NULL AND spend_lease_id IS NOT NULL AND spend_lease_gen IS "
    "NOT NULL AND spend_lease_allocated_micro IS NOT NULL AND provisional_id IS NULL) OR "
    "(registration_kind = 'CLAIM' AND provisional_id IS NOT NULL AND authorization_id IS "
    "NULL AND spend_lease_id IS NULL AND spend_lease_gen IS NULL AND "
    "spend_lease_allocated_micro IS NULL AND terminal_at IS NOT NULL))"
)
OPEN_PHASE_CHECK = (
    "CONSTRAINT spend_lease_open_phase CHECK "
    "(phase IN ('candidate', 'recovering', 'open', 'done'))"
)


def _fixture(*, objects_exist: bool) -> ScriptFixture:
    count = "1" if objects_exist else "0"
    return ScriptFixture(
        env={
            "GCP_PROJECT_ID": "harness-project",
            "SPANNER_INSTANCE_ID": "harness-instance",
            "SPANNER_DATABASE_ID": "harness-database",
        },
        responses=(
            (r"INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES", "READ_WRITE"),
            (r"INFORMATION_SCHEMA\.(TABLES|COLUMNS|INDEXES)", count),
        ),
    )


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    objects_exist: bool,
) -> HarnessRun:
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, _fixture(objects_exist=objects_exist))
    harness = DeployScriptHarness(tmp_path / ("existing" if objects_exist else "fresh"))
    return harness.run(SCRIPT)


def _ddls(run: HarnessRun) -> list[str]:
    return [
        argument.removeprefix("--ddl=").replace(r"\n", "\n")
        for call in run.calls
        if call[:5] == ["gcloud", "spanner", "databases", "ddl", "update"]
        for argument in call
        if argument.startswith("--ddl=")
    ]


def _ddl_starting(ddls: list[str], prefix: str) -> str:
    matches = [ddl for ddl in ddls if ddl.startswith(prefix)]
    assert len(matches) == 1, matches
    return matches[0]


def _table_columns(ddl: str) -> dict[str, str]:
    body = ddl.split("(\n", 1)[1].split("\n  ) PRIMARY KEY", 1)[0]
    columns: dict[str, str] = {}
    for raw_line in body.splitlines():
        line = raw_line.strip().removesuffix(",")
        if not line or line.startswith("CONSTRAINT "):
            continue
        name, specification = line.split(" ", 1)
        columns[name] = specification
    return columns


def _normalize_sql(value: str) -> str:
    return " ".join(value.split())


def _assert_authorization_manifest(ddls: list[str]) -> None:
    alters = [ddl for ddl in ddls if ddl.startswith("ALTER TABLE tr_gateway_authorization")]
    actual: dict[str, str] = {}
    prefix = "ALTER TABLE tr_gateway_authorization ADD COLUMN "
    for ddl in alters:
        name, specification = ddl.removeprefix(prefix).split(" ", 1)
        actual[name] = specification
    assert actual == AUTHORIZATION_COLUMNS
    assert all("DEFAULT" not in specification for specification in actual.values())


def _assert_arbitration_index_is_non_unique(ddls: list[str]) -> None:
    index = _ddl_starting(
        ddls,
        "CREATE NULL_FILTERED INDEX spend_lease_scope_arbitration_by_authorization",
    )
    assert _normalize_sql(index) == (
        "CREATE NULL_FILTERED INDEX spend_lease_scope_arbitration_by_authorization "
        "ON spend_lease_scope_arbitration (authorization_id)"
    )
    assert "CREATE UNIQUE" not in index


def _assert_migration_order(workflow: str) -> None:
    migrate_schema = workflow.split("\n  migrate-schema:\n", 1)[1].split(
        "\n  sync-runtime-secrets:\n", 1
    )[0]
    retention = migrate_schema.index("migrate_request_retention.sh --apply")
    spend_lease = migrate_schema.index("migrate_spend_lease.sh")
    assert retention < spend_lease


@pytest.fixture
def fresh_ddls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    run = _run(tmp_path, monkeypatch, objects_exist=False)
    assert run.returncode == 0, summarise(run)
    return _ddls(run)


def test_spend_lease_migration_is_executable_and_unconditional() -> None:
    path = ROOT / SCRIPT
    script = path.read_text()

    assert os.access(path, os.X_OK)
    assert "SPANNER_INSTANCE_ID" in script
    assert "SPANNER_DATABASE_ID" in script
    assert "INFORMATION_SCHEMA.TABLES" in script
    assert "INFORMATION_SCHEMA.COLUMNS" in script
    assert "INFORMATION_SCHEMA.INDEXES" in script
    assert "--apply" not in script


def test_authorization_alters_match_manifest_and_stage_d_adds_exactly_seven(
    fresh_ddls: list[str],
) -> None:
    _assert_authorization_manifest(fresh_ddls)
    assert list(AUTHORIZATION_COLUMNS)[-7:] == [
        "started_at",
        "heartbeat_seq",
        "heartbeat_at",
        "heartbeat_hash",
        "selected_endpoint_id",
        "delivered_usage",
        "pricing_snapshot",
    ]


def test_arbitration_table_matches_frozen_manifest(fresh_ddls: list[str]) -> None:
    table = _ddl_starting(fresh_ddls, "CREATE TABLE spend_lease_scope_arbitration")

    assert _table_columns(table) == ARBITRATION_COLUMNS
    assert "PRIMARY KEY (scope_salt, idempotency_scope)" in table
    assert ARBITRATION_SHAPE_CHECK in _normalize_sql(table)
    assert "ROW DELETION POLICY (OLDER_THAN(terminal_at, INTERVAL 30 DAY))" in table
    assert "created_at TIMESTAMP NOT NULL OPTIONS" not in table


def test_arbitration_secondary_index_is_named_sparse_and_non_unique(
    fresh_ddls: list[str],
) -> None:
    _assert_arbitration_index_is_non_unique(fresh_ddls)


def test_open_work_table_matches_frozen_manifest(fresh_ddls: list[str]) -> None:
    table = _ddl_starting(fresh_ddls, "CREATE TABLE spend_lease_open")

    assert _table_columns(table) == OPEN_COLUMNS
    assert "PRIMARY KEY (lease_id)" in table
    assert OPEN_PHASE_CHECK in _normalize_sql(table)
    assert "ROW DELETION POLICY" not in table


def test_done_row_with_null_next_attempt_is_absent_from_due_index(
    fresh_ddls: list[str],
) -> None:
    table = _ddl_starting(fresh_ddls, "CREATE TABLE spend_lease_open")
    index = _ddl_starting(fresh_ddls, "CREATE NULL_FILTERED INDEX spend_lease_open_due")

    assert "'done'" in table
    assert OPEN_COLUMNS["next_attempt_at"] == "TIMESTAMP"
    assert _normalize_sql(index) == (
        "CREATE NULL_FILTERED INDEX spend_lease_open_due "
        "ON spend_lease_open (next_attempt_at)"
    )


def test_indexes_are_waited_until_read_write(fresh_ddls: list[str]) -> None:
    script = (ROOT / SCRIPT).read_text()

    assert len(fresh_ddls) == 22
    for name in (
        "spend_lease_scope_arbitration_by_authorization",
        "spend_lease_open_due",
    ):
        create = script.index(f"if index_exists {name}")
        wait = script.index(f"wait_index_read_write {name}")
        assert create < wait


def test_fresh_apply_reports_every_object_created(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(tmp_path, monkeypatch, objects_exist=False)

    assert run.returncode == 0, summarise(run)
    assert len(_ddls(run)) == 22
    for object_name in (
        *(f"tr_gateway_authorization.{name}" for name in AUTHORIZATION_COLUMNS),
        "spend_lease_scope_arbitration",
        "spend_lease_scope_arbitration_by_authorization",
        "spend_lease_open",
        "spend_lease_open_due",
    ):
        assert f"{object_name}: created" in run.stdout
    index_state_reads = [
        call
        for call in run.calls
        if any("INDEX_STATE FROM INFORMATION_SCHEMA.INDEXES" in argument for argument in call)
    ]
    assert len(index_state_reads) == 2


def test_second_apply_is_idempotent_and_emits_no_ddl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _run(tmp_path, monkeypatch, objects_exist=True)

    assert run.returncode == 0, summarise(run)
    assert _ddls(run) == []
    for object_name in (
        *(f"tr_gateway_authorization.{name}" for name in AUTHORIZATION_COLUMNS),
        "spend_lease_scope_arbitration",
        "spend_lease_scope_arbitration_by_authorization",
        "spend_lease_open",
        "spend_lease_open_due",
    ):
        assert f"{object_name}: already present" in run.stdout


def test_deploy_runs_spend_lease_migration_after_request_retention() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()

    _assert_migration_order(workflow)


def test_mutation_guard_rejects_unique_arbitration_secondary_index(
    fresh_ddls: list[str],
) -> None:
    index = _ddl_starting(
        fresh_ddls,
        "CREATE NULL_FILTERED INDEX spend_lease_scope_arbitration_by_authorization",
    )
    mutated = [
        ddl.replace("CREATE NULL_FILTERED INDEX", "CREATE UNIQUE NULL_FILTERED INDEX")
        if ddl == index
        else ddl
        for ddl in fresh_ddls
    ]

    with pytest.raises(AssertionError):
        _assert_arbitration_index_is_non_unique(mutated)


def test_mutation_guard_rejects_spend_lease_before_request_retention() -> None:
    workflow = (ROOT / ".github/workflows/deploy.yml").read_text()
    migrate_schema = workflow.split("\n  migrate-schema:\n", 1)[1].split(
        "\n  sync-runtime-secrets:\n", 1
    )[0]
    retention_step = re.search(
        r"      - name: Apply bounded request-retention schema\n.*?"
        r"        run: scripts/deploy/migrate_request_retention.sh --apply\n",
        migrate_schema,
        re.DOTALL,
    )
    spend_step = re.search(
        r"      - name: Apply inert spend-lease schema\n.*?"
        r"        run: scripts/deploy/migrate_spend_lease.sh\n",
        migrate_schema,
        re.DOTALL,
    )
    assert retention_step is not None
    assert spend_step is not None
    mutated = workflow.replace(
        retention_step.group(0) + spend_step.group(0),
        spend_step.group(0) + retention_step.group(0),
    )

    with pytest.raises(AssertionError):
        _assert_migration_order(mutated)


def test_mutation_guard_rejects_allocated_money_default(fresh_ddls: list[str]) -> None:
    allocated = _ddl_starting(
        fresh_ddls,
        "ALTER TABLE tr_gateway_authorization ADD COLUMN spend_lease_allocated_micro",
    )
    mutated = [
        f"{ddl} DEFAULT (0)" if ddl == allocated else ddl
        for ddl in fresh_ddls
    ]

    with pytest.raises(AssertionError):
        _assert_authorization_manifest(mutated)


def test_stage_c_manifest_has_exactly_eleven_nullable_authorization_columns() -> None:
    script = (ROOT / SCRIPT).read_text()

    assert len(STAGE_C_NULLABLE_AUTHORIZATION_COLUMNS) == 11
    assert set(STAGE_C_NULLABLE_AUTHORIZATION_COLUMNS) <= set(AUTHORIZATION_COLUMNS)
    for name in STAGE_C_NULLABLE_AUTHORIZATION_COLUMNS:
        declaration = re.search(
            rf"ensure_column tr_gateway_authorization {name} \\\n+  \"([^\"]+)\"",
            script,
        )
        assert declaration is not None, name
        assert "NOT NULL" not in declaration.group(1)
