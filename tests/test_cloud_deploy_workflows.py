from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_aws_control_plane_installs_uv_before_running_completeness_gate() -> None:
    workflow = (ROOT / ".github/workflows/deploy-aws-control-plane.yml").read_text()

    setup_uv = workflow.index("uses: astral-sh/setup-uv@v7")
    deploy = workflow.index("run: bash ../ops/scripts/deploy/aws_ecs_control_plane.sh")

    assert setup_uv < deploy


def test_coordinator_operations_reuse_existing_wif_without_running_terraform() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/infra-apply.yml").read_text())
    jobs = workflow["jobs"]
    assert "inputs.operation == 'terraform'" in jobs["apply"]["if"]
    coordinator = jobs["release-coordinator"]
    assert "inputs.operation != 'terraform'" in coordinator["if"]
    assert "github.ref == 'refs/heads/main'" in coordinator["if"]
    steps = coordinator["steps"]
    assert not any("terraform" in step.get("run", "").lower() for step in steps)
    run = steps[-1]["run"]
    assert '[ "$CONFIRMATION" = APPLY ] || exit 1' in run
    assert "--clear-lifecycle" in run
    assert "gs://tr-deploy-mutex-quill-cloud-proxy" in run
    assert "--operation \"$OPERATION_ID\"" in run
    assert "storage rm" not in run


def test_aws_baked_release_selection_keeps_main_oidc_and_safety_gates() -> None:
    workflow = (ROOT / ".github/workflows/deploy-aws-control-plane.yml").read_text()
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "RELEASE_SHA: ${{ inputs.release_sha }}" in workflow
    assert '[[ "$RELEASE_SHA" =~ ^[0-9a-f]{40}$ ]]' in workflow
    assert 'git merge-base --is-ancestor "$RELEASE_SHA" HEAD' in workflow
    assert 'git checkout --detach "$RELEASE_SHA"' in workflow
    assert workflow.index("Select a merged release") < workflow.index("aws-actions/configure-aws-credentials")
    script = (ROOT / "scripts/deploy/aws_ecs_control_plane.sh").read_text()
    assert 'CI must pass for this exact release' in script
    assert 'cloud_bake_gate aws' in script
    assert 'require_cloud_complete aws' in script


@pytest.mark.parametrize("selection", ["baked", "default", "unmerged", "short", "shell"])
def test_aws_release_selection_executes_only_merged_full_shas(tmp_path: Path, selection: str) -> None:
    def git(*args: str) -> str:
        return subprocess.check_output(  # noqa: S603 -- local fixture git commands only
            [shutil.which("git") or "/usr/bin/git", *args], cwd=tmp_path, text=True,
        ).strip()

    git("init", "-q")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    git("commit", "-qm", "baked", "--allow-empty")
    baked = git("rev-parse", "HEAD")
    git("checkout", "-qb", "unmerged")
    git("commit", "-qm", "unmerged", "--allow-empty")
    unmerged = git("rev-parse", "HEAD")
    git("checkout", "--detach", baked)
    git("commit", "-qm", "main", "--allow-empty")
    head = git("rev-parse", "HEAD")
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy-aws-control-plane.yml").read_text())
    script = next(step["run"] for step in workflow["jobs"]["deploy"]["steps"]
                  if step.get("name") == "Select a merged release")
    values = {"baked": baked, "default": "", "unmerged": unmerged,
              "short": baked[:7], "shell": "$(touch injected)"}
    result = subprocess.run(  # noqa: S603 -- execute the repository workflow against a fixture repo
        [shutil.which("bash") or "/bin/bash", "-e", "-c", script], cwd=tmp_path,
        env={**os.environ, "RELEASE_SHA": values[selection]}, capture_output=True, text=True,
    )
    assert (result.returncode == 0) == (selection in {"baked", "default"})
    assert git("rev-parse", "HEAD") == (baked if selection == "baked" else head)
    assert not (tmp_path / "injected").exists()


def test_pull_request_validation_cannot_cancel_a_pending_infra_apply() -> None:
    """GitHub cancels a concurrency group's older pending run when a newer
    one joins. Validation of a pull request must therefore not share the
    apply's group, or a pull request pushed while an apply waits for a
    runner cancels it (2026-10-20: #1902's and #1903's applies). The exact
    expression is pinned: near misses that still mention both groups (an
    extra `&& 'infra-apply'`, `||` for `&&`) send pull requests back into
    the apply's group."""
    workflow = yaml.safe_load((ROOT / ".github/workflows/infra-apply.yml").read_text())
    assert workflow["concurrency"] == {
        "group": (
            "${{ github.event_name == 'pull_request' && "
            "format('infra-validate-{0}', github.event.pull_request.number) || 'infra-apply' }}"
        ),
        "cancel-in-progress": False,
    }


def test_settle_worker_release_job_inherits_parent_fence_and_prepared_checkout() -> None:
    jobs = yaml.safe_load((ROOT / ".github/workflows/deploy-production.yml").read_text())["jobs"]
    worker = jobs["settle-drain-worker"]
    assert worker["needs"] == ["rollout-secondaries", "admit-cloud"]
    assert worker["if"] == "github.repository == 'Lore-Hex/quill-router'"
    assert worker["continue-on-error"] is False
    assert worker["env"]["TR_DEPLOY_MUTEX_OPERATION"] == "${{ needs.admit-cloud.outputs.TR_DEPLOY_MUTEX_OPERATION }}"
    assert worker["env"]["TR_DEPLOY_MUTEX_CLOUD"] == "gcp"
    assert worker["env"]["GH_TOKEN"] == "${{ github.token }}"  # noqa: S105 - workflow expression
    steps = worker["steps"]
    assert steps[0] == {"uses": "actions/checkout@v7", "with": {"ref": "${{ inputs.sha }}"}}
    assert any(s.get("uses") == "google-github-actions/auth@v3" for s in steps)
    assert steps[-1]["run"] == "bash scripts/deploy/settle_drain_worker.sh"
    assert "if" not in steps[-1] and "continue-on-error" not in steps[-1]
    assert "settle-drain-worker" in jobs["finalize-cloud"]["needs"]
    script = (ROOT / "scripts/deploy/settle_drain_worker.sh").read_text()
    assert "deploy_mutex_acquire" not in script and "deploy_mutex_release" not in script
    assert script.index('cloud_bake_gate gcp') < script.index('gc run deploy')
    assert script.count("\ndeploy_mutex_assert\n") == 2


@pytest.mark.parametrize("hotfix", [False, True])
def test_worker_uses_same_release_hotfix_decision_as_parent(hotfix: bool) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy-production.yml").read_text())
    # PyYAML's YAML 1.1 parser reads the workflow's `on` key as True.
    assert workflow[True]["workflow_call"]["inputs"]["hotfix"] == {
        "type": "boolean", "default": False,
    }
    jobs = workflow["jobs"]
    assert jobs["settle-drain-worker"]["env"]["TR_SETTLE_DRAIN_HOTFIX"] == "${{ inputs.hotfix == true }}"
    parent = jobs["gate-on-ci"]["steps"][0]["run"]
    # Execute the parent's decision prefix: hotfix exits before CI; normal
    # releases fall through to CI. The script harness covers pending CI in both.
    prefix = parent[:parent.index('echo "waiting for CI')]
    script = prefix.replace("${{ inputs.hotfix }}", str(hotfix).lower()) + "echo CI_REQUIRED\n"
    result = subprocess.run(  # noqa: S603 - local workflow decision, no cloud commands
        [shutil.which("bash") or "/bin/bash", "-c", script], capture_output=True, text=True, check=True,
    )
    warning = "::warning::HOTFIX MODE — skipping the CI-green gate."
    assert (warning in result.stdout) is hotfix
    assert ("CI_REQUIRED" in result.stdout) is not hotfix
    assert warning in (ROOT / "scripts/deploy/settle_drain_worker.sh").read_text()
