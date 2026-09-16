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
    deploy = workflow.index("run: bash scripts/deploy/aws_ecs_control_plane.sh")

    assert setup_uv < deploy


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
