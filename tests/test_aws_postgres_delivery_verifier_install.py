"""The AWS-EU delivery verifier, installed by aws_eu_clickhouse_drain_install.sh.

Two layers. The unit, timer and installer text are pinned where a careless
edit would silently change what runs on the node. The installer's control
flow is then EXECUTED under the deploy-script harness's recording stubs, with
the SSM answers given per step: these tests prove what the script does with
each answer (refuse, print the operator step, reach the gate), not that the
node behaves that way. The drain installer as a whole stays NOT_PROVEN in
ROLLOUT_REGISTRY for exactly that reason; nothing here registers a fixture
for it globally, so test_the_unprovable_script_really_is_unprovable still
sees the default-fixture run stop before the gate.
"""

from __future__ import annotations

from collections.abc import Iterator
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
SCRIPT = "scripts/deploy/aws_eu_clickhouse_drain_install.sh"
INSTALL = (ROOT / SCRIPT).read_text()
UNIT = (ROOT / "clickhouse/tr-clickhouse-postgres-delivery.service").read_text()
TIMER = (ROOT / "clickhouse/tr-clickhouse-postgres-delivery.timer").read_text()
DRAIN_UNIT = (ROOT / "clickhouse/tr-clickhouse-operational-ingest-postgres.service").read_text()
HISTORY = "/var/lib/tr-clickhouse-ingest/postgres-delivery-aws.jsonl"
NODE_ROLE = "tr-eu-clickhouse-role"


# ---------------------------------------------------------------------------
# What runs on the node.
# ---------------------------------------------------------------------------


def test_unit_runs_the_verifier_as_the_ingest_user_with_no_secret_in_argv() -> None:
    exec_start = next(line for line in UNIT.splitlines() if line.startswith("ExecStart="))

    assert exec_start == (
        "ExecStart=/opt/tr-clickhouse/venv/bin/python -m clickhouse.verify_postgres_delivery "
        f"--lookback-hours 24 --history-file {HISTORY}"
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
# What the installer ships and writes.
# ---------------------------------------------------------------------------


def test_venv_carries_the_verifier_and_it_imports_before_the_swap() -> None:
    assert "'httpx>=0.27.0'" in INSTALL
    smoke = INSTALL.index("./venv/bin/python -c 'import clickhouse.verify_postgres_delivery'")
    assert INSTALL.index("import smoke test (staging)") < smoke < INSTALL.index("drain: activate")


def test_verifier_env_file_carries_only_a_passwordless_tr_verifier_dsn() -> None:
    start = INSTALL.index('ssm "verifier: environment file"')
    body = INSTALL[start : INSTALL.index('"\n', INSTALL.index("ENVEOF\n", start) + 7)]
    lines = body[body.index("<<'ENVEOF'\n") + 11 : body.index("\nENVEOF")].splitlines()

    assert lines == [
        "TR_POSTGRES_DSN=host=${DSQL_HOST} port=5432 user=tr_verifier "
        "dbname=postgres sslmode=require"
    ]
    assert "chmod 600 '$VERIFIER_ENV_FILE'" in body
    assert "cut -d= -f1 '$VERIFIER_ENV_FILE'" in body


def test_activation_stops_the_verifier_before_rotating_its_tree() -> None:
    activate = INSTALL.index('ssm "drain: activate')
    stop = INSTALL.index("systemctl stop $VERIFIER_TIMER $VERIFIER_SERVICE")
    rotate = INSTALL.index("rm -rf '${REMOTE_ROOT}.previous'")

    assert activate < stop < rotate


def test_scope_check_prints_only_names_counts_and_exception_classes() -> None:
    start = INSTALL.index('ssm "verifier: tr_verifier read scope"')
    snippet = INSTALL[start : INSTALL.index("PYEOF\n\")", start)]

    assert "source.read_scope()" in snippet
    assert "type(exc).__name__" in snippet
    assert "str(exc)" not in snippet and "repr(" not in snippet
    assert "('tr_verifier', True, 0)" in snippet
    # It reads the env files itself and prints none of their values.
    assert "print(env" not in snippet and "print(value" not in snippet


def test_verifier_steps_follow_the_proven_drain_and_precede_the_gate() -> None:
    order = [
        'ssm "drain: verify"',
        "grep -qx 'dsql:DbConnect'",
        'ssm "verifier: environment file"',
        'ssm "verifier: tr_verifier read scope"',
        'ssm "verifier: install units"',
        'ssm "verifier: run once"',
        'require_cloud_complete aws "$NEXT_STEPS"',
    ]
    positions = [INSTALL.index(marker) for marker in order]
    assert positions == sorted(positions)


def test_run_once_refuses_on_a_failed_or_unrecorded_check() -> None:
    start = INSTALL.index('ssm "verifier: run once"')
    snippet = INSTALL[start : INSTALL.index("PYEOF\n\"", start)]

    assert "systemctl start $VERIFIER_SERVICE || rc=\\$?" in snippet
    assert "refusing" in snippet and "exit 1" in snippet
    assert "--property=Result --value)\\\" = success" in snippet
    assert "--property=ExecMainStatus --value)\\\" = 0" in snippet
    assert "last.get('ok') is not True or checked < int(sys.argv[2])" in snippet
    assert "|| true" not in snippet


# ---------------------------------------------------------------------------
# The installer's control flow, executed.
# ---------------------------------------------------------------------------


def _fixture(*, decision: str = "dsql:DbConnect", failing_step: str = "") -> ScriptFixture:
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
            (r"ssm send-command .*--comment verifier: tr_verifier read scope ", "cid-scope"),
            (r"ssm send-command ", "cid-ok"),
            *failing,
            (r"get-command-invocation .*--query Status", "Success"),
            (
                r"get-command-invocation .*--command-id cid-capture .*StandardOutputContent",
                "install_started_at=1760000000\npre_install_pid=0",
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


@pytest.fixture(scope="module")
def harness(tmp_path_factory: pytest.TempPathFactory) -> DeployScriptHarness:
    return DeployScriptHarness(tmp_path_factory.mktemp("verifier-install-harness"))


@pytest.fixture
def run_with(
    harness: DeployScriptHarness, monkeypatch: pytest.MonkeyPatch
) -> Iterator[object]:
    def run(fixture: ScriptFixture, verifier_rc: int = 0) -> HarnessRun:
        monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, fixture)
        return harness.run(SCRIPT, verifier_rc=verifier_rc, timeout=300)

    yield run


def _ssm_steps(run: HarnessRun) -> list[str]:
    return [
        call[call.index("--comment") + 1]
        for call in run.calls
        if call[:3] == ["aws", "ssm", "send-command"]
    ]


def _verifier_steps(run: HarnessRun) -> list[str]:
    return [step for step in _ssm_steps(run) if step.startswith("verifier:")]


def test_a_clean_install_runs_every_verifier_step_then_the_gate(run_with) -> None:
    run = run_with(_fixture())

    assert run.returncode == 0, summarise(run)
    assert _verifier_steps(run) == [
        "verifier: environment file",
        "verifier: tr_verifier read scope",
        "verifier: install units",
        "verifier: run once",
    ]
    steps = _ssm_steps(run)
    assert steps.index("drain: verify") < steps.index("verifier: environment file")
    assert run.gate_ran_for("aws")
    assert run.cloud_cli_calls_after_the_gate() == []
    assert "tr_verifier scope: role=tr_verifier view_select=True" in run.stderr


def test_a_failed_verifier_run_refuses_before_the_gate(run_with) -> None:
    run = run_with(_fixture(failing_step="verifier: run once"))

    assert run.returncode != 0, summarise(run)
    assert _verifier_steps(run)[-1] == "verifier: run once"
    assert "SSM step failed (verifier: run once)" in run.stderr
    assert not run.gate_ran_for("aws")


def test_an_unprovisioned_role_prints_the_operator_step_and_installs_nothing(run_with) -> None:
    run = run_with(_fixture(failing_step="verifier: tr_verifier read scope"))

    assert run.returncode != 0, summarise(run)
    assert _verifier_steps(run) == [
        "verifier: environment file",
        "verifier: tr_verifier read scope",
    ]
    assert "OPERATOR STEP" in run.stderr
    assert f"-v node_role_arn=arn:aws:iam::330422590279:role/{NODE_ROLE}" in run.stderr
    assert "-f scripts/deploy/aws_eu_tr_verifier_role.sql" in run.stderr
    # The reviewed SQL itself, not a paraphrase of it.
    assert "GRANT SELECT ON tr_verifier_generations TO tr_verifier;" in run.stderr
    assert "AWS IAM GRANT tr_verifier TO %L" in run.stderr
    assert 'PGPASSWORD="$(aws dsql generate-db-connect-admin-auth-token' in run.stderr
    assert not run.gate_ran_for("aws")


def test_a_node_without_db_connect_is_refused_before_any_verifier_step(run_with) -> None:
    run = run_with(_fixture(decision="dsql:DbConnectAdmin"))

    assert run.returncode != 0, summarise(run)
    assert "drain: verify" in _ssm_steps(run)
    assert _verifier_steps(run) == []
    assert f"{NODE_ROLE} lacks dsql:DbConnect" in run.stderr
    assert not run.gate_ran_for("aws")


def test_the_gate_status_still_decides_the_exit_after_a_clean_verifier(run_with) -> None:
    """Positive control for the refusals above: with every step answered
    Success, the only remaining failure is the gate's, returned unaltered."""
    run = run_with(_fixture(), verifier_rc=2)

    assert run.gate_ran_for("aws")
    assert run.returncode == 2, summarise(run)
