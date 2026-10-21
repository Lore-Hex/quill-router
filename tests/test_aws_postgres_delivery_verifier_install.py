"""The AWS-EU delivery verifier and its installer.

scripts/deploy/aws_eu_postgres_delivery_verifier_install.sh installs it.
The drain installer only keeps the verifier's tree importable and its timer
running across the tree swap. It must never fail because of the verifier: a
new optional component must not make an existing install path fail.

Two layers. The unit, timer and script text are pinned where a careless edit
would silently change what runs on the node. Both installers' control flow is
then EXECUTED under the deploy-script harness's recording stubs, with the SSM
answers given per step. These tests prove what each script does with each
answer (refuse, print the operator step, succeed), not that the node behaves
that way. Nothing here registers a fixture globally, so
test_the_unprovable_script_really_is_unprovable still sees the default-fixture
drain run stop before the gate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from .deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    HarnessRun,
    ScriptFixture,
    summarise,
)

ROOT = Path(__file__).resolve().parents[1]
VERIFIER_SCRIPT = "scripts/deploy/aws_eu_postgres_delivery_verifier_install.sh"
DRAIN_SCRIPT = "scripts/deploy/aws_eu_clickhouse_drain_install.sh"
VERIFIER = (ROOT / VERIFIER_SCRIPT).read_text()
DRAIN = (ROOT / DRAIN_SCRIPT).read_text()
UNIT = (ROOT / "clickhouse/tr-clickhouse-postgres-delivery.service").read_text()
TIMER = (ROOT / "clickhouse/tr-clickhouse-postgres-delivery.timer").read_text()
DRAIN_UNIT = (ROOT / "clickhouse/tr-clickhouse-operational-ingest-postgres.service").read_text()
HISTORY = "/var/lib/tr-clickhouse-ingest/postgres-delivery-aws.jsonl"
NODE_ROLE = "tr-eu-clickhouse-role"
VERIFIER_STEPS = [
    "verifier: drain tree present",
    "verifier: environment file",
    "verifier: tr_verifier read scope",
    "verifier: install units",
    "verifier: run once",
]


# ---------------------------------------------------------------------------
# What runs on the node.
# ---------------------------------------------------------------------------


def test_unit_runs_the_verifier_as_the_ingest_user_with_no_secret_in_argv() -> None:
    exec_start = next(line for line in UNIT.splitlines() if line.startswith("ExecStart="))

    assert exec_start == (
        "ExecStart=/opt/tr-clickhouse/venv/bin/python -m clickhouse.verify_postgres_delivery "
        f"--lookback-hours 24 --scan-limit 1000000 --history-file {HISTORY}"
    )
    for secret_shaped in ("--dsn", "password", "PASSWORD", "token", "$"):
        assert secret_shaped not in exec_start
    assert "Type=oneshot" in UNIT
    assert "User=tr-clickhouse-ingest" in UNIT and "Group=tr-clickhouse-ingest" in UNIT
    # The same flat tree and hardening as the drain it shares a venv with.
    for line in (
        "WorkingDirectory=/opt/tr-clickhouse",
        "Environment=HOME=/var/lib/tr-clickhouse-ingest",
        "ProtectSystem=strict",
        "ProtectHome=true",
        "NoNewPrivileges=true",
        "ReadWritePaths=/var/lib/tr-clickhouse-ingest",
    ):
        assert line in UNIT and line in DRAIN_UNIT
    assert "Environment=PYTHONPATH" not in UNIT


def test_scan_cap_covers_the_whole_history_not_just_the_window() -> None:
    """The source scan walks all generation history by primary key, so a cap
    below the plane's total count fails every run as incomplete."""
    exec_start = next(line for line in UNIT.splitlines() if line.startswith("ExecStart="))
    scan_limit = int(exec_start.split("--scan-limit ")[1].split()[0])

    assert scan_limit >= 1_000_000
    assert "--limit " not in exec_start  # the window cap keeps its default
    assert "TimeoutStartSec=20m" in UNIT


def test_unit_reads_the_drain_env_first_so_only_the_dsn_is_overridden() -> None:
    files = [line for line in UNIT.splitlines() if line.startswith("EnvironmentFile=")]

    assert files == [
        "EnvironmentFile=/etc/tr-clickhouse-ingest-postgres.env",
        "EnvironmentFile=/etc/tr-clickhouse-postgres-delivery.env",
    ]
    assert "EnvironmentFile=/etc/tr-clickhouse-ingest-postgres.env" in DRAIN_UNIT


def test_timer_runs_hourly() -> None:
    assert "OnUnitActiveSec=1h" in TIMER
    assert "Persistent=true" in TIMER
    assert "WantedBy=timers.target" in TIMER


# ---------------------------------------------------------------------------
# What the drain installer keeps, and what it must not do.
# ---------------------------------------------------------------------------


def test_drain_venv_carries_the_verifier_and_it_imports_before_the_swap() -> None:
    assert "'httpx>=0.27.0'" in DRAIN
    smoke = DRAIN.index("./venv/bin/python -c 'import clickhouse.verify_postgres_delivery'")
    assert DRAIN.index("import smoke test (staging)") < smoke < DRAIN.index("drain: activate")


def test_drain_stops_the_verifier_before_the_swap_and_restarts_it_only_if_enabled() -> None:
    activate = DRAIN.index('ssm "drain: activate')
    stop = DRAIN.index("systemctl stop $VERIFIER_TIMER $VERIFIER_SERVICE")
    rotate = DRAIN.index("rm -rf '${REMOTE_ROOT}.previous'")
    restart_guard = DRAIN.index("if systemctl is-enabled --quiet $VERIFIER_TIMER 2>/dev/null; then")
    restart = DRAIN.index("  systemctl start $VERIFIER_TIMER\n")

    assert activate < stop < rotate < DRAIN.index('ssm "drain: install unit"')
    assert DRAIN.index('ssm "drain: install unit"') < restart_guard < restart


def test_drain_installer_never_installs_or_runs_the_verifier() -> None:
    assert 'ssm "verifier:' not in DRAIN
    assert "read_scope" not in DRAIN
    assert "systemctl start $VERIFIER_SERVICE" not in DRAIN
    assert "systemctl enable $VERIFIER_TIMER" not in DRAIN
    # It names the verifier installer as the next step, inside its summary.
    summary = DRAIN[DRAIN.index("cat <<EOF\n\nInstalled.") :]
    assert "bash scripts/deploy/aws_eu_postgres_delivery_verifier_install.sh" in summary


# ---------------------------------------------------------------------------
# What the verifier installer writes and checks.
# ---------------------------------------------------------------------------


def test_verifier_env_file_carries_only_a_passwordless_tr_verifier_dsn() -> None:
    start = VERIFIER.index('ssm "verifier: environment file"')
    body = VERIFIER[start : VERIFIER.index('"\n', VERIFIER.index("ENVEOF\n", start) + 7)]
    lines = body[body.index("<<'ENVEOF'\n") + 11 : body.index("\nENVEOF")].splitlines()

    assert lines == [
        "TR_POSTGRES_DSN=host=${DSQL_HOST} port=5432 user=tr_verifier "
        "dbname=postgres sslmode=require"
    ]
    assert "chmod 600 '$VERIFIER_ENV_FILE'" in body
    assert "cut -d= -f1 '$VERIFIER_ENV_FILE'" in body


def test_verifier_installer_ships_no_code_and_requires_the_drain_tree() -> None:
    assert "tar " not in VERIFIER and "send-command" in VERIFIER
    start = VERIFIER.index('ssm "verifier: drain tree present"')
    snippet = VERIFIER[start : VERIFIER.index('")"; then', start)]
    assert "test -x '$REMOTE_ROOT/venv/bin/python'" in snippet
    assert "test -f '$DRAIN_ENV_FILE'" in snippet
    assert "./venv/bin/python -c 'import clickhouse.verify_postgres_delivery'" in snippet


def test_scope_check_prints_only_names_counts_and_exception_classes() -> None:
    start = VERIFIER.index('ssm "verifier: tr_verifier read scope"')
    snippet = VERIFIER[start : VERIFIER.index("PYEOF\n\")", start)]

    assert "source.read_scope()" in snippet
    assert "type(exc).__name__" in snippet
    assert "str(exc)" not in snippet and "repr(" not in snippet
    assert "('tr_verifier', True, 0)" in snippet
    assert "print(env" not in snippet and "print(value" not in snippet


def test_verifier_steps_run_in_fail_closed_order() -> None:
    order = [
        "grep -qx 'dsql:DbConnect'",
        *(f'ssm "{step}"' for step in VERIFIER_STEPS),
    ]
    positions = [VERIFIER.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_run_once_refuses_on_a_failed_or_unrecorded_check() -> None:
    start = VERIFIER.index('ssm "verifier: run once"')
    snippet = VERIFIER[start : VERIFIER.index("PYEOF\n\"", start)]

    assert "systemctl start $VERIFIER_SERVICE || rc=\\$?" in snippet
    assert "refusing" in snippet and "exit 1" in snippet
    assert "--property=Result --value)\\\" = success" in snippet
    assert "--property=ExecMainStatus --value)\\\" = 0" in snippet
    assert "last.get('ok') is not True or checked < int(sys.argv[2])" in snippet
    assert "|| true" not in snippet


def test_verifier_installer_is_not_a_completeness_gate_script() -> None:
    """It is optional observability, not a bring-up step: it must not carry the
    cloud gate, which would also bind it into ROLLOUT_REGISTRY."""
    assert "verify_cloud_complete" not in VERIFIER
    assert "require_cloud_complete" not in VERIFIER
    assert (ROOT / VERIFIER_SCRIPT).stat().st_mode & 0o111


# ---------------------------------------------------------------------------
# Both installers' control flow, executed.
# ---------------------------------------------------------------------------


def _fixture(*, decision: str = "dsql:DbConnect", failing_step: str = "") -> ScriptFixture:
    """`failing_step` is a regex over the SSM comment whose command fails."""
    failing: tuple[tuple[str, str], ...] = ()
    if failing_step:
        failing = ((r"get-command-invocation .*--command-id cid-failing .*--query Status", "Failed"),)
    return ScriptFixture(
        env={"INSTANCE_ID": "i-0harness", "CHUNK_BYTES": "100000000"},
        responses=(
            *(
                ((rf"ssm send-command .*--comment {failing_step} ", "cid-failing"),)
                if failing_step
                else ()
            ),
            (r"ssm send-command .*--comment drain: capture running process ", "cid-capture"),
            (r"ssm send-command .*--comment verifier: drain tree present ", "cid-tree"),
            (r"ssm send-command .*--comment verifier: tr_verifier read scope ", "cid-scope"),
            (r"ssm send-command ", "cid-ok"),
            *failing,
            (r"get-command-invocation .*--query Status", "Success"),
            (
                r"get-command-invocation .*--command-id cid-capture .*StandardOutputContent",
                "install_started_at=1760000000\npre_install_pid=0",
            ),
            (
                r"get-command-invocation .*--command-id cid-tree .*StandardOutputContent",
                "install_started_at=1760000000",
            ),
            (
                r"get-command-invocation .*--command-id cid-scope .*StandardOutputContent",
                "role=tr_verifier view_select=True other_readable_relations=0",
            ),
            (r"get-command-invocation ", ""),
            (
                r"ec2 describe-instances .*IamInstanceProfile",
                "arn:aws:iam::330422590279:instance-profile/tr-eu-clickhouse-instance-profile",
            ),
            (r"iam get-instance-profile ", NODE_ROLE),
            (r"iam simulate-principal-policy ", decision),
        ),
    )


Runner = Callable[..., HarnessRun]


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> DeployScriptHarness:
    return DeployScriptHarness(tmp_path_factory.mktemp("verifier-install-harness"))


@pytest.fixture
def run_with(harness: DeployScriptHarness, monkeypatch: pytest.MonkeyPatch) -> Iterator[Runner]:
    def run(script: str, fixture: ScriptFixture, verifier_rc: int = 0) -> HarnessRun:
        monkeypatch.setitem(SCRIPT_FIXTURES, script, fixture)
        return harness.run(script, verifier_rc=verifier_rc, timeout=300)

    yield run


def _ssm_steps(run: HarnessRun) -> list[str]:
    return [
        call[call.index("--comment") + 1]
        for call in run.calls
        if call[:3] == ["aws", "ssm", "send-command"]
    ]


def test_a_clean_verifier_install_runs_every_step_and_exits_zero(run_with: Runner) -> None:
    """Positive control for every refusal below: same fixture, nothing failing."""
    run = run_with(VERIFIER_SCRIPT, _fixture())

    assert run.returncode == 0, summarise(run)
    assert _ssm_steps(run) == VERIFIER_STEPS
    assert "tr_verifier scope: role=tr_verifier view_select=True" in run.stderr
    assert "Delivery verifier installed and its first run was clean." in run.stdout
    assert not run.verifier_calls


def test_a_failed_verifier_run_refuses(run_with: Runner) -> None:
    run = run_with(VERIFIER_SCRIPT, _fixture(failing_step="verifier: run once"))

    assert run.returncode != 0, summarise(run)
    assert _ssm_steps(run) == VERIFIER_STEPS
    assert "SSM step failed (verifier: run once)" in run.stderr
    assert "first run was clean" not in run.stdout


def test_an_unprovisioned_role_prints_the_operator_step_and_installs_nothing(
    run_with: Runner,
) -> None:
    run = run_with(VERIFIER_SCRIPT, _fixture(failing_step="verifier: tr_verifier read scope"))

    assert run.returncode != 0, summarise(run)
    assert _ssm_steps(run) == VERIFIER_STEPS[:3]
    assert "OPERATOR STEP" in run.stderr
    assert f"-v node_role_arn=arn:aws:iam::330422590279:role/{NODE_ROLE}" in run.stderr
    assert "-f scripts/deploy/aws_eu_tr_verifier_role.sql" in run.stderr
    # The reviewed SQL itself, not a paraphrase of it.
    assert "GRANT SELECT ON tr_verifier_generations TO tr_verifier;" in run.stderr
    assert "AWS IAM GRANT tr_verifier TO %L" in run.stderr
    assert 'PGPASSWORD="$(aws dsql generate-db-connect-admin-auth-token' in run.stderr


def test_a_missing_drain_tree_is_refused_before_anything_is_written(run_with: Runner) -> None:
    run = run_with(VERIFIER_SCRIPT, _fixture(failing_step="verifier: drain tree present"))

    assert run.returncode != 0, summarise(run)
    assert _ssm_steps(run) == VERIFIER_STEPS[:1]
    assert "the drain is not installed" in run.stderr
    assert "bash scripts/deploy/aws_eu_clickhouse_drain_install.sh" in run.stderr


def test_a_node_without_db_connect_is_refused_before_any_ssm_step(run_with: Runner) -> None:
    run = run_with(VERIFIER_SCRIPT, _fixture(decision="dsql:DbConnectAdmin"))

    assert run.returncode != 0, summarise(run)
    assert _ssm_steps(run) == []
    assert f"{NODE_ROLE} lacks dsql:DbConnect" in run.stderr


def test_drain_install_succeeds_when_the_tr_verifier_role_is_missing(run_with: Runner) -> None:
    """Every verifier step failing (no role, no grant) must not touch the drain
    install: it issues none of them, reaches the gate, and returns its status."""
    fixture = _fixture(decision="dsql:DbConnectAdmin", failing_step="verifier: .*")

    run = run_with(DRAIN_SCRIPT, fixture)

    assert run.returncode == 0, summarise(run)
    assert run.gate_ran_for("aws")
    assert not [step for step in _ssm_steps(run) if step.startswith("verifier:")]
    assert "drain: verify" in _ssm_steps(run)
    assert "bash scripts/deploy/aws_eu_postgres_delivery_verifier_install.sh" in run.stdout
    # And the gate, not the verifier, still decides the drain installer's exit.
    assert run_with(DRAIN_SCRIPT, fixture, verifier_rc=2).returncode == 2
