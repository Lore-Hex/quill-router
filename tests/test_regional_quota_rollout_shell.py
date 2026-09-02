from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL_TEST = ROOT / "tests" / "shell" / "test_regional_quota_rollout.sh"


def test_regional_quota_rollout_shell_contract() -> None:
    assert os.access(SHELL_TEST, os.X_OK), "regional quota shell contract must be executable"
    result = subprocess.run(  # noqa: S603 - fixed repository-owned executable
        [str(SHELL_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "10 passed" in result.stdout
