"""Lore-Hex runs no third-party GitHub Actions.

Every `uses:` in .github/workflows and .github/actions must be one of:
  * a GitHub-owned action (actions/*, github/*) pinned by full commit SHA with a
    `# vX.Y.Z` comment;
  * this repository's own composite action, pinned by full commit SHA:
    Lore-Hex/quill-router/.github/actions/<name>@<sha>;
  * a local reusable workflow (./.github/workflows/<file>) or, only in
    actions-selftest.yml, a local action (./.github/actions/<name>), which
    tests the action from the pull request's own tree.
The repository's Actions setting (allowed_actions=selected, GitHub-owned plus
Lore-Hex/*) enforces the same boundary on GitHub's side.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
OWN_ACTIONS = "Lore-Hex/quill-router/.github/actions/"
GITHUB_OWNED = re.compile(r"(actions|github)/[A-Za-z0-9._-]+(/[A-Za-z0-9._/-]+)?@[0-9a-f]{40}")
OWN = re.compile(re.escape(OWN_ACTIONS) + r"([a-z0-9-]+)@[0-9a-f]{40}")
USES_LINE = re.compile(r"^\s*(?:-\s+)?uses:\s*(?P<value>[^#\n]*?)\s*(?:#\s*(?P<comment>.*))?$")


def _files() -> list[Path]:
    return sorted([
        *(ROOT / ".github/workflows").glob("*.y*ml"),
        *(ROOT / ".github/actions").glob("*/action.y*ml"),
    ])


def _uses_lines(path: Path) -> list[tuple[int, str, str | None]]:
    found = []
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        match = USES_LINE.match(line)
        if match and not line.lstrip().startswith("#"):
            found.append((number, match["value"].strip().strip("'\""), match["comment"]))
    return found


def violations(root: Path = ROOT) -> list[str]:
    problems = []
    for path in _files() if root == ROOT else sorted([
        *(root / ".github/workflows").glob("*.y*ml"),
        *(root / ".github/actions").glob("*/action.y*ml"),
    ]):
        relative = path.relative_to(root).as_posix()
        for number, value, comment in _uses_lines(path):
            where = f"{relative}:{number}: {value}"
            if value.startswith("./.github/workflows/") and relative.startswith(".github/workflows/"):
                continue
            if value.startswith("./.github/actions/") and relative == ".github/workflows/actions-selftest.yml":
                if not (root / value / "action.yml").is_file():
                    problems.append(f"{where}: no such local action")
                continue
            if GITHUB_OWNED.fullmatch(value):
                if not (comment and re.fullmatch(r"v\d+\.\d+\.\d+", comment.strip())):
                    problems.append(f"{where}: a GitHub-owned action needs a '# vX.Y.Z' comment")
                continue
            own = OWN.fullmatch(value)
            if own:
                if not (root / ".github/actions" / own[1] / "action.yml").is_file():
                    problems.append(f"{where}: no .github/actions/{own[1]}/action.yml in this repository")
                continue
            problems.append(f"{where}: not a SHA-pinned GitHub-owned or Lore-Hex action")
    return problems


def test_every_action_is_github_owned_or_our_own_and_pinned_by_sha() -> None:
    assert violations() == []


def test_the_workflows_use_one_pin_for_the_repository_actions() -> None:
    # One reviewed commit of .github/actions at a time; moving the pin is one edit.
    pins = {
        value.rsplit("@", 1)[1]
        for path in (ROOT / ".github/workflows").glob("*.y*ml")
        for _, value, _ in _uses_lines(path)
        if value.startswith(OWN_ACTIONS)
    }
    assert len(pins) == 1, pins


def test_composite_actions_name_no_other_action() -> None:
    # The repository's actions are shell only, so the SHA pin fixes all the code they run.
    for path in (ROOT / ".github/actions").glob("*/action.y*ml"):
        action = yaml.safe_load(path.read_text())
        assert action["runs"]["using"] == "composite", path
        assert all("uses" not in step for step in action["runs"]["steps"]), path


@pytest.mark.parametrize("uses, accepted", [
    ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1", True),
    ("github/codeql-action/upload-sarif@6f2ac81f5cd81fe351a197ba0b00d91d03cd2aee # v4.39.0", True),
    (f"{OWN_ACTIONS}setup-uv@0123456789abcdef0123456789abcdef01234567 # main", True),
    ("actions/checkout@v7", False),
    ("actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1", False),
    ("astral-sh/setup-uv@v7", False),
    ("google-github-actions/auth@0123456789abcdef0123456789abcdef01234567 # v3.0.0", False),
    (f"{OWN_ACTIONS}setup-uv@main", False),
    (f"{OWN_ACTIONS}no-such-action@0123456789abcdef0123456789abcdef01234567 # main", False),
    ("Lore-Hex/other-repo/.github/actions/x@0123456789abcdef0123456789abcdef01234567 # main", False),
    ("./.github/actions/setup-uv", False),
    ("docker://alpine:3", False),
])
def test_the_policy_rejects_what_it_should(tmp_path: Path, uses: str, accepted: bool) -> None:
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/actions/setup-uv").mkdir(parents=True)
    (tmp_path / ".github/actions/setup-uv/action.yml").write_text("runs: {using: composite, steps: []}\n")
    (tmp_path / ".github/workflows/x.yml").write_text(f"jobs:\n  j:\n    steps:\n      - uses: {uses}\n")
    assert (violations(tmp_path) == []) is accepted
