from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHELL_TEST = ROOT / "tests" / "shell" / "test_active_revision.sh"


def test_active_revision_shell_contract() -> None:
    assert os.access(SHELL_TEST, os.X_OK), "active revision shell contract must be executable"
    result = subprocess.run(  # noqa: S603 - fixed repository-owned executable
        [str(SHELL_TEST)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "8 passed" in result.stdout


def test_active_revision_helpers_live_in_the_sourced_side_effect_free_file() -> None:
    helpers = (ROOT / "scripts/deploy/_active_revision.sh").read_text()
    library = (ROOT / "scripts/deploy/_lib.sh").read_text()
    for name in ("active_revision_json", "revision_env", "resolve_image_digest"):
        assert f"\n{name}() {{" in helpers
        assert f"\n{name}() {{" not in library
    assert 'source "${_TR_LIB_DIR}/_active_revision.sh"' in library
    # Sourcing the helpers alone must not reach a cloud: the shell contract
    # above depends on it, and so does every phase script's `bash -n`.
    assert "PROJECT_NUMBER=" not in helpers
    assert "$(gc " not in helpers.split("resolve_image_digest() {")[0]
