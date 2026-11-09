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


def _stubbed_get(responses: dict[str, tuple[int, object]]):
    calls: list[str] = []

    def _get(path: str, token: str, *, attempts: int = 4) -> tuple[int, object]:
        calls.append(path)
        return responses[path]

    return _get, calls


def _branch_view(contexts: list[str], *, protected: bool = True) -> dict[str, object]:
    # GitHub returns the protection block for protected and unprotected
    # branches alike; an unprotected branch reads protected false with an
    # empty block.
    return {
        "name": "main", "protected": protected,
        "protection": {"enabled": protected,
                       "required_status_checks": {"contexts": contexts, "checks": []}},
    }


@pytest.mark.parametrize(
    ("view", "expected_problems", "expected_note"),
    [
        (_branch_view(["lint", "test (1)", "test (2)", "test (3)", "CI"]), [],
         "required checks read from the branch view"),
        (_branch_view(["lint", "test (1)", "test (2)", "test (3)"]),
         ["quill-router: required status checks missing ['CI'] (have ['lint', 'test (1)', 'test (2)', 'test (3)'])"],
         "required checks read from the branch view"),
        (_branch_view([], protected=False), ["quill-router: main is NOT PROTECTED"],
         "branch view read: main is not protected"),
        ({"name": "main", "protected": True}, [],
         "branch view has no protection block: required checks NOT asserted"),
    ],
)
def test_required_checks_are_read_from_the_public_branch_view_when_protection_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, view: dict[str, object], expected_problems: list[str],
    expected_note: str,
) -> None:
    # The audit's token cannot read /branches/main/protection (403). The branch
    # view is public and names the required checks, so a missing check is a
    # real problem even then; force-push and deletion stay unreadable.
    module = _load_script()
    monkeypatch.setattr(module, "PROTECTED_REPOS", {"quill-router": module.PROTECTED_REPOS["quill-router"]})
    get, calls = _stubbed_get({
        "/repos/Lore-Hex/quill-router/branches/main/protection": (403, {"message": "Resource not accessible"}),
        "/repos/Lore-Hex/quill-router/branches/main": (200, view),
    })
    monkeypatch.setattr(module, "_get", get)
    problems, unreadable = module.check_branch_protection("Lore-Hex", "test-token")
    assert problems == expected_problems
    assert unreadable == [
        "quill-router: could not read protection (HTTP 403) — force-push, deletion "
        f"and admin enforcement NOT asserted; {expected_note}"
    ]
    assert calls == [
        "/repos/Lore-Hex/quill-router/branches/main/protection",
        "/repos/Lore-Hex/quill-router/branches/main",
    ]


def test_an_unreadable_branch_view_stays_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_script()
    monkeypatch.setattr(module, "PROTECTED_REPOS", {"quill-router": module.PROTECTED_REPOS["quill-router"]})
    get, _calls = _stubbed_get({
        "/repos/Lore-Hex/quill-router/branches/main/protection": (403, {"message": "Resource not accessible"}),
        "/repos/Lore-Hex/quill-router/branches/main": (503, None),
    })
    monkeypatch.setattr(module, "_get", get)
    problems, unreadable = module.check_branch_protection("Lore-Hex", "test-token")
    assert problems == []
    assert unreadable == [
        "quill-router: could not read protection (HTTP 403) — force-push, deletion "
        "and admin enforcement NOT asserted; branch view unreadable (HTTP 503): required checks NOT asserted"
    ]


def test_readable_protection_does_not_consult_the_branch_view(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load_script()
    monkeypatch.setattr(module, "PROTECTED_REPOS", {"quill-router": module.PROTECTED_REPOS["quill-router"]})
    get, calls = _stubbed_get({
        "/repos/Lore-Hex/quill-router/branches/main/protection": (200, {
            "required_status_checks": {"contexts": sorted(module.PROTECTED_REPOS["quill-router"])},
            "allow_force_pushes": {"enabled": False}, "allow_deletions": {"enabled": False},
            "enforce_admins": {"enabled": False}, "required_pull_request_reviews": None,
        }),
    })
    monkeypatch.setattr(module, "_get", get)
    problems, unreadable = module.check_branch_protection("Lore-Hex", "test-token")
    assert problems == [] and unreadable == []
    assert calls == ["/repos/Lore-Hex/quill-router/branches/main/protection"]
