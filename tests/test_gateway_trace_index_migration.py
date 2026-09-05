from pathlib import Path

import pytest

from tests.deploy_script_harness import SCRIPT_FIXTURES, DeployScriptHarness, ScriptFixture
from tests.test_spend_lease_migration import _ddls

SCRIPT = "scripts/deploy/migrate_gateway_request_index.sh"


@pytest.mark.parametrize(
    "mode,exists,ready,old,expected,success",
    [
        ("--prepare", "0", "1", "1", ["CREATE NULL_FILTERED INDEX"], True),
        ("--prepare", "1", "1", "1", [], True),
        ("--prepare", "1", "0", "1", [], False),
        ("--prepare", "garbled", "1", "1", [], False),
        ("--retire-unique", "0", "0", "1", [], False),
        ("--retire-unique", "1", "0", "1", [], False),
        ("--retire-unique", "1", "1", "1", ["DROP INDEX"], True),
        ("--retire-unique", "1", "1", "0", [], True),
        ("--retire-unique", "1", "1", "garbled", [], False),
        ("--bad-mode", "1", "1", "1", [], False),
    ],
)
def test_trace_index_migration_preserves_data_and_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    mode: str, exists: str, ready: str, old: str,
    expected: list[str], success: bool,
) -> None:
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, ScriptFixture(
        env={"GCP_PROJECT_ID": "test", "SPANNER_INSTANCE_ID": "test", "SPANNER_DATABASE_ID": "test"},
        responses=(
            (r"INDEX_STATE='READ_WRITE'", ready),
            (r"INDEX_NAME='tr_gateway_authorization_by_trace_id'", exists),
            (r"INDEX_NAME='tr_gateway_authorization_by_gateway_request_id'", old),
        ),
    ))
    run = DeployScriptHarness(tmp_path).run(SCRIPT, args=(mode,))
    assert (run.returncode == 0) is success, run.stderr
    ddls = _ddls(run)
    assert len(ddls) == len(expected)
    for ddl, prefix in zip(ddls, expected, strict=True):
        assert ddl.startswith(prefix)
        assert "DROP TABLE" not in ddl
    if mode == "--prepare":
        assert not any("DROP" in ddl for ddl in ddls)
