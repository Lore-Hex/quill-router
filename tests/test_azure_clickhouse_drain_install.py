"""Execution contracts for the Azure operational-analytics drain installer."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from .deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    ScriptFixture,
    summarise,
)

SCRIPT = "scripts/deploy/azure_clickhouse_drain_install.sh"
ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "clickhouse/tr-clickhouse-operational-ingest-postgres.service"


def _fixture(
    *, delivery_reply: str = "rows advanced: 10 -> 11\n__TR_RUNCMD_OK__"
) -> ScriptFixture:
    return ScriptFixture(
        env={"VERSIONER_PERL_VERSION": "5.34"},
        responses=(
            (r"delivery proof: waiting", delivery_reply),
            (r"vm run-command invoke", "__TR_RUNCMD_OK__"),
        )
    )


def _harness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fixture: ScriptFixture | None = None,
) -> DeployScriptHarness:
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, fixture or _fixture())
    harness = DeployScriptHarness(tmp_path / "harness")
    git = shutil.which("git")
    assert git is not None
    commands = (
        ("init", "-q"),
        ("config", "user.email", "test@trustedrouter.com"),
        ("config", "user.name", "TrustedRouter Test"),
        ("add", "clickhouse", "src/trusted_router"),
        ("commit", "-qm", "harness worker snapshot"),
    )
    for command in commands:
        subprocess.run(  # noqa: S603 - resolved system git, fixed test arguments
            [git, *command],
            cwd=harness.mirror,
            check=True,
            capture_output=True,
        )
    return harness


def _joined_calls(run: object) -> list[str]:
    return [" ".join(call) for call in run.calls]  # type: ignore[attr-defined]


def test_preflight_authenticates_to_postgres_and_clickhouse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _harness(tmp_path, monkeypatch).run(SCRIPT)

    assert run.returncode == 0, summarise(run)
    preflight = next(
        call
        for call in _joined_calls(run)
        if "vm run-command invoke" in call and "psql" in call
    )
    assert "PGPASSWORD=" in preflight
    assert "psql" in preflight and "SELECT 1" in preflight
    assert "CLICKHOUSE_PASSWORD=" in preflight
    assert "clickhouse-client" in preflight
    assert "__TR_RUNCMD_OK__" in preflight
    secret_checks = [
        call
        for call in _joined_calls(run)
        if "keyvault secret show" in call
    ]
    assert len(secret_checks) == 2


def test_installed_unit_is_notify_watchdog_managed() -> None:
    unit = UNIT.read_text()

    assert "Type=notify" in unit
    assert "NotifyAccess=main" in unit
    assert "WatchdogSec=600" in unit
    assert "Restart=always" in unit


def test_final_verification_requires_a_clickhouse_row_count_to_advance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _harness(tmp_path, monkeypatch).run(SCRIPT)

    assert run.returncode == 0, summarise(run)
    proof = next(
        call
        for call in _joined_calls(run)
        if "vm run-command invoke" in call and "delivery proof" in call
    )
    assert "SELECT sum(c)" in proof
    for table in (
        "activity_generations",
        "synthetic_probe_samples",
        "client_request_events",
        "client_minute_counters",
    ):
        assert table in proof
    assert "after" in proof and "before" in proof
    assert "-gt" in proof
    assert "exit 1" in proof
    assert "__TR_RUNCMD_OK__" in proof


def test_no_row_movement_makes_the_installer_fail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _harness(
        tmp_path,
        monkeypatch,
        _fixture(delivery_reply="row count did not advance"),
    ).run(SCRIPT)

    assert run.returncode != 0
    assert "delivery proof: the remote script failed" in run.stderr


def test_every_remote_step_requires_the_success_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _harness(tmp_path, monkeypatch).run(SCRIPT)

    assert run.returncode == 0, summarise(run)
    remote_calls = [
        call for call in _joined_calls(run) if "vm run-command invoke" in call
    ]
    assert remote_calls
    assert all("__TR_RUNCMD_OK__" in call for call in remote_calls)


def test_passwords_reach_clients_only_through_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _harness(tmp_path, monkeypatch).run(SCRIPT)

    assert run.returncode == 0, summarise(run)
    calls = "\n".join(_joined_calls(run))
    assert "PGPASSWORD=" in calls
    assert "CLICKHOUSE_PASSWORD=" in calls
    assert "CH_PASSWORD=%s" in calls
    assert "TR_POSTGRES_DSN=host=%s" in calls
    assert "CH_PASSWORD length=" in calls
    assert "PGPASSWORD length=" in calls
    assert "CH_PASSWORD is a literal command; refusing" in calls
    assert "PGPASSWORD is a literal command; refusing" in calls
    assert "cut -d= -f1" in calls
    assert "--password " not in calls
    assert "--value " not in calls


def test_payload_is_a_checksummed_committed_head_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = _harness(tmp_path, monkeypatch)

    run = harness.run(SCRIPT)

    assert run.returncode == 0, summarise(run)
    calls = _joined_calls(run)
    joined = "\n".join(calls)
    assert "sha256 mismatch" in joined
    assert "AppleDouble sidecars survived" in joined
    smoke = next(i for i, call in enumerate(calls) if "import smoke test" in call or "CONFIG_EXIT_CODE" in call)
    swap = next(i for i, call in enumerate(calls) if "swap into place" in call or "installed at /opt/tr-clickhouse" in call)
    assert smoke < swap
    assert "systemctl restart tr-clickhouse-operational-ingest-postgres.service" in joined
    installed = harness.mirror / "scripts/deploy/azure_clickhouse_drain_install.sh"
    script = installed.read_text()
    assert 'source "${SCRIPT_DIR}/_clickhouse_bundle.sh"' in script
    assert 'build_clickhouse_bundle "$ROOT" "$archive"' in script
    assert 'tar czf "$WORK/drain.tgz"' not in script
