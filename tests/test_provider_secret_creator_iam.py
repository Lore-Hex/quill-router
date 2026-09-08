from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/deploy/provider_secret_creator_iam.sh"
ROLE = "projects/test-project/roles/trustedRouterProviderSecretCreator"


def run_bootstrap(
    tmp_path: Path, *args: str, existing: bool = False, denied: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    binary = tmp_path / "gcloud"
    calls = tmp_path / "calls"
    binary.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$CALLS"
if [ -n "$DENIED" ] && [[ "$*" == *"$DENIED"* ]]; then
  echo PERMISSION_DENIED >&2
  exit 1
fi
case "$*" in
  *"iam roles list"*|*"projects get-iam-policy"*)
    if [ "$EXISTING" = 1 ]; then printf '%s\\n' "$ROLE"; fi ;;
  *"iam roles create"*|*"iam roles update"*|*"projects add-iam-policy-binding"*) ;;
  *) exit 2 ;;
esac
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    result = subprocess.run(  # noqa: S603 - fixed local script, fake gcloud
        ["/bin/bash", str(SCRIPT), *args],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "PROJECT_ID": "test-project",
            "CALLS": str(calls),
            "ROLE": ROLE,
            "EXISTING": "1" if existing else "0",
            "DENIED": denied,
        },
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text().splitlines() if calls.exists() else []


def test_dry_run_only_reads_iam(tmp_path: Path) -> None:
    result, calls = run_bootstrap(tmp_path)
    assert result.returncode == 0, result.stderr
    assert len(calls) == 2
    assert "[dry-run]" in result.stdout
    assert "creation granted" not in result.stdout


@pytest.mark.parametrize("existing", [False, True])
def test_apply_grants_only_create_permission(tmp_path: Path, existing: bool) -> None:
    result, calls = run_bootstrap(tmp_path, "--apply", existing=existing)
    assert result.returncode == 0, result.stderr
    mutation = calls[2]
    assert f"iam roles {'update' if existing else 'create'}" in mutation
    assert "--permissions=secretmanager.secrets.create" in mutation.split()
    assert len(calls) == (3 if existing else 4)
    if not existing:
        assert f"--role={ROLE}" in calls[3]
        assert "--member=serviceAccount:tr-deploy@test-project.iam.gserviceaccount.com" in calls[3]
        assert "--condition=None" in calls[3]


@pytest.mark.parametrize("denied", ["iam roles list", "projects get-iam-policy", "iam roles create"])
def test_iam_failure_stops_before_binding(tmp_path: Path, denied: str) -> None:
    result, calls = run_bootstrap(tmp_path, "--apply", denied=denied)
    assert result.returncode != 0
    assert "PERMISSION_DENIED" in result.stderr
    assert not any("add-iam-policy-binding" in call for call in calls)
    assert "creation granted" not in result.stdout


def test_rejects_extra_arguments_without_cloud_access(tmp_path: Path) -> None:
    result, calls = run_bootstrap(tmp_path, "--apply", "unexpected")
    assert result.returncode == 2
    assert not calls
