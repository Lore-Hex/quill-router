from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts/security/check_repo_security_coverage.py"
WORKFLOW = ROOT / ".github/workflows/check-repo-security-coverage.yml"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("repo_security_coverage", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("extra_args", "expected"),
    [
        ([], 1),
        (["--allow-unreadable-branch-protection"], 0),
    ],
)
def test_branch_protection_unreadable_has_a_narrow_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    extra_args: list[str],
    expected: int,
) -> None:
    module = _load_script()
    monkeypatch.setenv("REPO_SECURITY_TOKEN", "test-token")
    monkeypatch.setattr(module, "check_branch_protection", lambda *_args: ([], ["403"]))
    monkeypatch.setattr(module, "list_public_repos", lambda *_args: ["repo"])
    monkeypatch.setattr(module, "pvr_state", lambda *_args: module.COVERED)
    monkeypatch.setattr(module, "security_md_state", lambda *_args: module.COVERED)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *extra_args])

    assert module.main() == expected


def test_real_security_gap_stays_fatal_when_unreadable_is_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_script()
    monkeypatch.setenv("REPO_SECURITY_TOKEN", "test-token")
    monkeypatch.setattr(module, "check_branch_protection", lambda *_args: ([], ["403"]))
    monkeypatch.setattr(module, "list_public_repos", lambda *_args: ["repo"])
    monkeypatch.setattr(module, "pvr_state", lambda *_args: module.UNCOVERED)
    monkeypatch.setattr(module, "security_md_state", lambda *_args: module.COVERED)
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "--allow-unreadable-branch-protection"],
    )

    assert module.main() == 1


def test_workflow_closes_stale_issue_without_masking_coverage() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'ARGS="--allow-unreadable-branch-protection"' in workflow
    assert "Close the resolved coverage issue" in workflow
    assert 'if: success()' in workflow
    assert "gh issue close" in workflow
    assert workflow.count("continue-on-error: true") >= 2
