"""The operator reporting account stays loopback-only, read-only and narrow."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/deploy/clickhouse_operator_reader.sh"
CONTROL = ROOT / "scripts/deploy/clickhouse_control_reader.sh"


def _users_xml(script: str) -> str:
    return script.split("<<'XML'", 1)[1].split("\nXML\n", 1)[0]


def test_operator_reader_grants_exactly_the_reporting_tables() -> None:
    xml = _users_xml(SCRIPT.read_text())
    granted = set(re.findall(r"<query>GRANT SELECT ON tr\.(\w+)</query>", xml))
    assert granted == {"activity_generations", "provider_benchmark_samples", "workspace_directory"}
    # Nothing but table-scoped SELECT grants.
    assert len(re.findall(r"<query>", xml)) == len(granted)
    assert "GRANT ALL" not in xml
    assert "tr_entities" not in xml


def test_operator_reader_is_read_only_and_loopback_only() -> None:
    xml = _users_xml(SCRIPT.read_text())
    assert "<readonly>1</readonly>" in xml
    assert set(re.findall(r"<ip>([^<]+)</ip>", xml)) == {"127.0.0.1", "::1"}
    assert "<host" not in xml and "<host_regexp" not in xml


def test_operator_reader_never_puts_the_password_on_a_command_line() -> None:
    script = SCRIPT.read_text()
    assert "--password" not in script
    assert "password_sha256_hex" in script
    # The self-check reads the password from stdin and also proves writes fail.
    assert 'IFS= read -r P; export CLICKHOUSE_PASSWORD=' in script
    assert "tr_operator_read can write" in script


def test_operator_reader_does_not_restart_clickhouse() -> None:
    assert "systemctl restart" not in SCRIPT.read_text()


def test_control_reader_still_lacks_workspace_directory() -> None:
    # The runtime credential stays pinned to what the app queries.
    assert "workspace_directory" not in CONTROL.read_text()


def test_operator_secret_is_not_granted_to_runtime_service_accounts() -> None:
    secret = "trustedrouter-clickhouse-operator-read-password"
    for path in (ROOT / "scripts/deploy").glob("*.sh"):
        if path == SCRIPT:
            continue
        assert secret not in path.read_text(), path
