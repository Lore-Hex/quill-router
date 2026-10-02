"""The AWS deploy renews its session once the deploy mutex is held.

aws-actions/configure-aws-credentials mints a one-hour STS session when the job
starts, and the deploy mutex can hold the AWS control-plane deploy for up to an
hour (TR_DEPLOY_WAIT_SECONDS=3600) before the ECS rollout begins. A session
that expires mid-rollout fails the rollout, and the script's rollback runs on
the same expired session. GCP is unaffected: its workload-identity credential
fetches a fresh GitHub token whenever it needs one.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from .test_aws_ecs_release import CLI_STUB, run_ecs_fixture

ROOT = Path(__file__).resolve().parents[1]
ROLE = "arn:aws:iam::330422590279:role/tr-router-github-deploy"
JOB_START_SESSION = "job-start-session"
ACTIONS_ENV = {
    "ACTIONS_ID_TOKEN_REQUEST_URL": "https://token.actions.example/request?api-version=2.0",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "harness-request-token",
    "ECS_REQUEST_TOKEN": "harness-request-token",
    "ECS_OIDC_TOKEN": "harness-oidc-token",
    "AWS_DEPLOY_ROLE_ARN": ROLE,
    "GITHUB_RUN_ID": "4242",
}


def _run(tmp_path: Path, env: dict[str, str]):
    sessions = tmp_path / "aws-sessions"
    extra = {
        "AWS_ACCESS_KEY_ID": "ASIAJOBSTART",
        "AWS_SECRET_ACCESS_KEY": "job-start-secret",
        "AWS_SESSION_TOKEN": JOB_START_SESSION,
        "ECS_AWS_SESSIONS": str(sessions),
        **env,
    }
    result, recorded = run_ecs_fixture(tmp_path, extra_env=extra)
    acquired_file = tmp_path / "unlock.acquired"
    acquired = int(acquired_file.read_text()) if acquired_file.exists() else None
    used = (
        [json.loads(line) for line in sessions.read_text().splitlines()]
        if sessions.exists()
        else []
    )
    return result, recorded, acquired, used


def test_in_actions_the_session_is_renewed_once_the_lease_is_held(tmp_path: Path) -> None:
    result, recorded, acquired, used = _run(tmp_path, ACTIONS_ENV)

    assert result.returncode == 0, result.stderr
    assert acquired is not None
    renewal = recorded.index(
        next(c for c in recorded if c[:3] == ["aws", "sts", "assume-role-with-web-identity"])
    )
    token_request = recorded.index(next(c for c in recorded if c[0] == "curl"))
    first_write = recorded.index(
        next(c for c in recorded if c[0] == "docker" or c[:3] == ["aws", "ecs", "update-service"])
    )
    # The renewal comes after the lease is taken and before any image push or
    # service update, so the rollout and any rollback run on the new session.
    assert acquired <= token_request < renewal < first_write
    assume = recorded[renewal]
    assert assume[assume.index("--role-arn") + 1] == ROLE
    assert assume[assume.index("--role-session-name") + 1] == "tr-deploy-4242"
    assert assume[assume.index("--duration-seconds") + 1] == "3600"
    assert "--no-sign-request" in assume
    curl = recorded[token_request]
    assert curl[-1] == ACTIONS_ENV["ACTIONS_ID_TOKEN_REQUEST_URL"] + "&audience=sts.amazonaws.com"
    # Neither the request token nor the ID token is in any command's argv.
    flat = json.dumps(recorded)
    assert "harness-request-token" not in flat
    assert "harness-oidc-token" not in flat
    # Every AWS call before the renewal used the job's first session, and
    # every call after it the renewed one.
    renewal_use = used.index(["sts assume-role-with-web-identity", JOB_START_SESSION])
    assert {token for _, token in used[:renewal_use]} == {JOB_START_SESSION}
    after = used[renewal_use + 1 :]
    assert after and {token for _, token in after} == {"renewed-session"}
    assert ["ecs update-service", "renewed-session"] in after


def test_outside_actions_the_operator_session_is_left_alone(tmp_path: Path) -> None:
    env = {k: v for k, v in ACTIONS_ENV.items() if not k.startswith("ACTIONS_ID_TOKEN_")}
    result, recorded, _, used = _run(tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert not any(c[0] == "curl" for c in recorded)
    assert not any(c[:3] == ["aws", "sts", "assume-role-with-web-identity"] for c in recorded)
    assert {token for _, token in used} == {JOB_START_SESSION}


def _assert_stopped_before_any_write(result, recorded, tmp_path: Path) -> None:
    assert result.returncode != 0
    assert not any(c[0] == "docker" for c in recorded)
    assert not any(c[:3] == ["aws", "ecs", "update-service"] for c in recorded)
    assert not any(c[:3] == ["aws", "ecs", "register-task-definition"] for c in recorded)
    # The lease is released with the failure, as for any failed deploy.
    assert (tmp_path / "unlock").read_text().strip() == str(result.returncode)


def test_a_failed_renewal_stops_before_any_production_write(tmp_path: Path) -> None:
    result, recorded, _, _ = _run(tmp_path, {**ACTIONS_ENV, "ECS_FAIL_ASSUME": "1"})

    _assert_stopped_before_any_write(result, recorded, tmp_path)
    assert "renewing the AWS session after the deploy mutex wait failed" in result.stderr


def test_a_failed_token_request_stops_before_any_production_write(tmp_path: Path) -> None:
    result, recorded, _, _ = _run(tmp_path, {**ACTIONS_ENV, "ECS_FAIL_OIDC": "1"})

    _assert_stopped_before_any_write(result, recorded, tmp_path)
    assert "could not fetch a fresh GitHub OIDC token to renew the AWS session" in result.stderr
    assert not any(c[:3] == ["aws", "sts", "assume-role-with-web-identity"] for c in recorded)


def test_a_missing_role_stops_before_any_production_write(tmp_path: Path) -> None:
    env = {k: v for k, v in ACTIONS_ENV.items() if k != "AWS_DEPLOY_ROLE_ARN"}
    result, recorded, _, _ = _run(tmp_path, env)

    _assert_stopped_before_any_write(result, recorded, tmp_path)
    assert "AWS_DEPLOY_ROLE_ARN must name the deploy role" in result.stderr
    assert not any(c[0] == "curl" for c in recorded)


def test_the_workflow_renews_with_the_role_it_assumed_in_both_modes() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy-aws-control-plane.yml").read_text())
    job = workflow["jobs"]["deploy"]
    assert job["env"]["AWS_DEPLOY_ROLE_ARN"] == ROLE
    login = next(
        step
        for step in job["steps"]
        if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@")
    )
    assert login["with"]["role-to-assume"] == "${{ env.AWS_DEPLOY_ROLE_ARN }}"
    assert workflow["permissions"]["id-token"] == "write"
    # The deploy mode renews inside aws_ecs_control_plane.sh, right after its
    # own acquire; the drain-install mode acquires in the workflow itself.
    script = (ROOT / "scripts/deploy/aws_ecs_control_plane.sh").read_text()
    assert script.index("deploy_mutex_acquire\n") < script.index("aws_refresh_session || exit 1")
    drain = next(
        step
        for step in job["steps"]
        if step.get("name") == "Install outbox drain and verify cloud completeness"
    )
    run = drain["run"]
    assert "source ../ops/scripts/deploy/_aws_session.sh" in run
    acquire = run.index("deploy_mutex_acquire\n")
    renew = run.index("aws_refresh_session || exit 1")
    assert acquire < renew < run.index("aws_eu_clickhouse_schema_apply.sh")


def test_tracing_never_prints_the_tokens_or_the_new_session(tmp_path: Path) -> None:
    env = {**ACTIONS_ENV, "GITHUB_ACTIONS": "true", "SHELLOPTS": "xtrace"}
    result, _, _, _ = _run(tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert "+ aws_refresh_session" in result.stderr  # the caller really was traced
    for secret in (
        "harness-request-token",
        "harness-oidc-token",
        "renewed-secret",
        "renewed-session",
    ):
        assert secret not in result.stderr
    # The new session is registered for masking, as the first one was.
    masks = [line for line in result.stdout.splitlines() if line.startswith("::add-mask::")]
    assert masks == [
        "::add-mask::ASIARENEWED",
        "::add-mask::renewed-secret",
        "::add-mask::renewed-session",
    ]


def test_a_short_token_transfer_stops_before_any_production_write(tmp_path: Path) -> None:
    result, recorded, _, _ = _run(tmp_path, {**ACTIONS_ENV, "ECS_PARTIAL_OIDC": "1"})

    _assert_stopped_before_any_write(result, recorded, tmp_path)
    assert "could not fetch a fresh GitHub OIDC token to renew the AWS session" in result.stderr


@pytest.mark.parametrize(("partial", "renewed"), [(False, True), (True, False)])
def test_the_renewal_checks_each_step_without_pipefail(
    tmp_path: Path, partial: bool, renewed: bool
) -> None:
    """GitHub's default step shell is `bash -e` without pipefail, as in drain-install."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("curl", "aws"):
        (bin_dir / tool).write_text(CLI_STUB)
        (bin_dir / tool).chmod(0o755)
    (tmp_path / "state").write_text("{}")
    (tmp_path / "calls").write_text("")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("ECS_", "ACTIONS_ID_TOKEN_", "AWS_", "SHELLOPTS"))
    }
    env.update(ACTIONS_ENV)
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "ECS_STATE": str(tmp_path / "state"),
            "ECS_CALLS": str(tmp_path / "calls"),
        }
    )
    if partial:
        env["ECS_PARTIAL_OIDC"] = "1"
    library = ROOT / "scripts/deploy/_aws_session.sh"
    result = subprocess.run(  # noqa: S603 - the repository's library, all cloud tools stubbed
        [
            shutil.which("bash") or "/bin/bash",
            "-e",
            "-c",
            f'source "{library}"; aws_refresh_session; echo "renewed=$AWS_SESSION_TOKEN"',
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert (result.returncode == 0) == renewed, result.stderr
    assert ("renewed=renewed-session" in result.stdout) == renewed


def test_an_unparseable_sts_response_never_reaches_the_log(tmp_path: Path) -> None:
    result, recorded, _, _ = _run(tmp_path, {**ACTIONS_ENV, "ECS_MALFORMED_STS": "1"})

    _assert_stopped_before_any_write(result, recorded, tmp_path)
    assert (
        "renewing the AWS session after the deploy mutex wait failed (aws exit 255)"
        in result.stderr
    )
    for secret in ("ASIARENEWED", "renewed-secret", "renewed-session"):
        assert secret not in result.stderr
        assert secret not in result.stdout


def test_an_identity_check_that_fails_after_printing_stops_the_deploy(tmp_path: Path) -> None:
    result, recorded, _, _ = _run(tmp_path, {**ACTIONS_ENV, "ECS_CALLER_FAILS_AFTER_RENEWAL": "1"})

    _assert_stopped_before_any_write(result, recorded, tmp_path)
    assert "the renewed AWS session is not in account 330422590279" in result.stderr
