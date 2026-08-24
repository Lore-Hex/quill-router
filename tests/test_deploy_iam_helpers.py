from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _fake_gcloud(tmp_path: Path) -> tuple[Path, Path]:
    binary = tmp_path / "gcloud"
    calls = tmp_path / "calls.log"
    binary.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >>"${FAKE_GCLOUD_CALLS}"
case "$*" in
  *"projects describe"*)
    printf '123456\\n'
    ;;
  *"projects get-iam-policy"*)
    if [ "${FAKE_BINDING_PRESENT:-0}" = "1" ]; then
      printf '%s\\n' "${FAKE_ROLE}"
    fi
    ;;
  *"projects add-iam-policy-binding"*)
    if [ "${FAKE_ADD_DENIED:-0}" = "1" ]; then
      printf 'ERROR: PERMISSION_DENIED: Policy update access denied. setIamPolicy\\n' >&2
      exit 1
    fi
    ;;
  *)
    printf 'unexpected fake gcloud call: %s\\n' "$*" >&2
    exit 2
    ;;
esac
""",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    return binary, calls


def _run_ensure_project_role(
    tmp_path: Path,
    *,
    binding_present: bool,
    add_denied: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    _, calls = _fake_gcloud(tmp_path)
    role = "roles/run.developer"
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "PROJECT_ID": "test-project",
        "FAKE_GCLOUD_CALLS": str(calls),
        "FAKE_BINDING_PRESENT": "1" if binding_present else "0",
        "FAKE_ADD_DENIED": "1" if add_denied else "0",
        "FAKE_ROLE": role,
    }
    result = subprocess.run(  # noqa: S603 - fixed local test command
        [
            "/bin/bash",
            "-c",
            (
                "source scripts/deploy/_lib.sh; "
                "ensure_project_role "
                "'serviceAccount:runtime@test-project.iam.gserviceaccount.com' "
                f"'{role}'"
            ),
        ],
        cwd=ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    return result, calls.read_text(encoding="utf-8").splitlines()


def test_existing_project_role_is_read_without_policy_write(tmp_path: Path) -> None:
    result, calls = _run_ensure_project_role(tmp_path, binding_present=True)

    assert result.returncode == 0, result.stderr
    assert sum("get-iam-policy" in call for call in calls) == 1
    assert not any("add-iam-policy-binding" in call for call in calls)


def test_missing_project_role_fails_after_one_denied_write(tmp_path: Path) -> None:
    result, calls = _run_ensure_project_role(
        tmp_path,
        binding_present=False,
        add_denied=True,
    )

    assert result.returncode != 0
    assert sum("add-iam-policy-binding" in call for call in calls) == 1
    assert "Policy update access denied" in result.stderr
