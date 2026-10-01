import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conformance.spanner_ddl import DDL, SOURCE_DIGESTS
from tests.conformance.spanner_schema_source import assert_schema_matches
from tests.deploy_script_harness import SCRIPT_FIXTURES, DeployScriptHarness, ScriptFixture
from tests.migration_ddl import recorded_ddls
from trusted_router.storage_gcp_trust import TRUST_EVENT_COLUMNS

SCRIPT = "scripts/deploy/migrate_trust_event_debt_index.sh"
INDEX = "tr_trust_event_by_debt"


def carrier_ddls(tmp_path, monkeypatch, state="READ_WRITE"):
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, ScriptFixture(
        env={"GCP_PROJECT_ID": "test", "SPANNER_INSTANCE_ID": "test",
             "SPANNER_DATABASE_ID": "test", "TR_DEBT_INDEX_WAIT_ATTEMPTS": "2",
             "TR_DEBT_INDEX_WAIT_SECONDS": "0"},
        responses=((r"INFORMATION_SCHEMA.INDEXES", state),),
    ))
    run = DeployScriptHarness(tmp_path).run(SCRIPT)
    return run, recorded_ddls(run)


def test_debt_index_carrier_is_additive_and_idempotent(tmp_path, monkeypatch):
    # Execute the actual shell carrier twice; the server owns IF NOT EXISTS.
    for attempt in range(2):
        run, ddls = carrier_ddls(tmp_path / str(attempt), monkeypatch)
        assert run.returncode == 0, run.stderr
        assert len(ddls) == 1
        sql = " ".join(ddls[0].split())
        assert sql.startswith(f"CREATE INDEX IF NOT EXISTS {INDEX} ON tr_trust_event "
                              "(workspace_id, kind, unrecovered_micro)")
        assert sql.replace("IF NOT EXISTS ", "") in DDL
        assert set(TRUST_EVENT_COLUMNS) <= set(re.findall(r"\w+", sql))
        assert "DROP" not in sql and "ALTER" not in sql


@pytest.mark.parametrize("state", ["WRITE_ONLY", "", "garbled"])
def test_debt_index_carrier_requires_backfill_completion(tmp_path, monkeypatch, state):
    run, _ = carrier_ddls(tmp_path, monkeypatch, state)
    assert run.returncode != 0
    assert "did not become READ_WRITE" in run.stderr


def test_debt_index_schema_source_matches_carrier():
    # This is the tenth mutation's oracle: removing the carrier statement must
    # fail even on hosts where the native emulator provides no plan statistics.
    assert_schema_matches(DDL, SOURCE_DIGESTS)
    assert any(sql.startswith(f"CREATE INDEX {INDEX} ON") for sql in DDL)
    assert SCRIPT in SOURCE_DIGESTS
    assert (Path(__file__).resolve().parents[1] / SCRIPT).stat().st_mode & 0o111


def test_native_c1_cases_are_selected_by_ci():
    result = subprocess.run(  # noqa: S603 - fixed offline collection, no fixtures run
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
         "tests/conformance/test_rpc_c1_native.py", "-k", "spanner-emulator"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    prefix = "tests/conformance/test_rpc_c1_native.py::"
    selected = {line.removeprefix(prefix) for line in result.stdout.splitlines() if line.startswith(prefix)}
    assert selected == {
        "test_payment_debt_positive_range_and_profile[backend=spanner-emulator]",
        *(f"test_finalize_nine_statement_native_readback[backend=spanner-emulator-{window}]"
          for window in ("current", "rollover", "null-starts")),
    }
