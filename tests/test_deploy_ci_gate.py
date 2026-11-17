"""Exercise the exact-SHA CI decision independently of GitHub's scheduler."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/deploy.yml"


def test_ci_wait_belongs_to_the_scheduler_not_a_runner():
    ci = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    gate = yaml.safe_load(WORKFLOW.read_text())["jobs"]["gate-on-ci"]
    assert ci["name"] == "CI"
    assert ci["concurrency"]["group"] == (
        "ci-${{ github.workflow }}-${{ github.ref }}-${{ github.ref == 'refs/heads/main' && github.sha || 'head' }}"
    )
    assert gate["needs"] == "build-image"
    assert gate["concurrency"] == {
        "group": "${{ github.ref == 'refs/heads/main' && inputs.hotfix != true && "
                 "format('ci-CI-refs/heads/main-{0}', github.sha) || format('deploy-check-{0}', github.run_id) }}",
        "queue": "max",
    }
    assert gate["timeout-minutes"] == 5
    assert gate["permissions"] == {"actions": "read"}


def _ci(event="push", status="completed", conclusion="success"):
    return {"event": event, "status": status, "conclusion": conclusion, "databaseId": 7}


@pytest.mark.parametrize(("rows", "allowed"), [
    ([_ci()], True),
    ([_ci("workflow_dispatch")], True),
    ([_ci("pull_request"), _ci("push", "in_progress", "")], False),
    ([_ci("pull_request", conclusion="failure"), _ci()], True),
    ([_ci("pull_request")], False),
    ([], False),
    ([_ci(status="queued", conclusion="success")], False),
    ([_ci(conclusion="skipped"), _ci()], False),
    ([_ci(conclusion="failure")], False),
    ([_ci(conclusion="cancelled")], False),
    ([_ci(status="pending", conclusion=None)], False),
    ("API error", False),
    ("malformed", False),
])
def test_exact_commit_checked_once_and_fails_closed(tmp_path, rows, allowed):
    gate = yaml.safe_load(WORKFLOW.read_text())["jobs"]["gate-on-ci"]
    script = gate["steps"][0]["run"].replace("${{ inputs.hotfix }}", "false").replace("${{ inputs.track_main }}", "false")
    gh = tmp_path / "gh"
    gh.write_text("""#!/bin/bash
set -eu
echo call >> "$CALLS"
[[ "$1 $2 $3 $4 $5 $6 $7 $8" == "run list --repo o/r --workflow ci.yml --commit abc123" ]]
[[ "$9" == --json && "${10}" == databaseId,status,conclusion,event && "${11}" == --jq ]]
case "$ROWS" in '"API error"') exit 1 ;; '"malformed"') echo 'bad JSON'; exit 0 ;; esac
jq -c "${12}" <<<"$ROWS"
""")
    gh.chmod(0o700)
    # An accidental return to polling fails immediately, including on an API outage.
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 99\n")
    sleep.chmod(0o700)
    calls = tmp_path / "calls"
    result = subprocess.run(  # noqa: S603 - workflow shell with fake gh; no network
        [shutil.which("bash") or "/bin/bash", "-c", script], capture_output=True, text=True, timeout=10,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}", "GITHUB_SHA": "abc123",
             "GITHUB_REPOSITORY": "o/r", "ROWS": json.dumps(rows), "CALLS": str(calls)},
    )
    assert (result.returncode == 0) is allowed, result.stdout + result.stderr
    assert result.returncode != 99
    assert calls.read_text().splitlines() == ["call"]


def shell_ci_lookups(paths: list[Path]) -> list[tuple[str, str]]:
    """Each `gh run list ... --workflow ci.yml ... --commit` command in the
    files, with its continuation lines."""
    import re

    found = []
    for path in paths:
        for match in re.finditer(r"gh run list(?:[^\n]*\\\n)*[^\n]*", path.read_text()):
            command = match.group(0)
            if "--workflow ci.yml" in command and "--commit" in command:
                found.append((str(path.relative_to(ROOT)), command))
    return found


def python_ci_lookups(paths: list[Path]) -> list[tuple[str, list[object]]]:
    """Each `run list` argument sequence naming ci.yml in the Python files:
    the string constants and names of a list or call that holds them."""
    import ast

    found = []
    for path in paths:
        tree = ast.parse(path.read_text())
        constants = {node.targets[0].id: node.value.value for node in tree.body
                     if isinstance(node, ast.Assign) and len(node.targets) == 1
                     and isinstance(node.targets[0], ast.Name) and isinstance(node.value, ast.Constant)}
        for node in ast.walk(tree):
            items = node.elts if isinstance(node, ast.List) else node.args if isinstance(node, ast.Call) else None
            if not items:
                continue
            values = [item.value if isinstance(item, ast.Constant) else
                      constants.get(item.id, item.id) if isinstance(item, ast.Name) else None for item in items]
            pairs = list(zip(values, values[1:], strict=False))
            if ("run", "list") in pairs and ("--workflow", "ci.yml") in pairs and "--commit" in values:
                found.append((str(path.relative_to(ROOT)), values))
    return found


def test_every_ci_run_lookup_counts_push_and_dispatch_runs_only() -> None:
    # Workflows and deploy scripts alike: a gate that takes the newest run of
    # any event could take a pull_request run, which checks a merge commit.
    workflows = sorted((ROOT / ".github/workflows").glob("*.yml"))
    scripts = sorted(path for path in (ROOT / "scripts").rglob("*") if path.suffix in (".sh", ".py", ""))
    shell = shell_ci_lookups([*workflows, *(path for path in scripts if path.is_file() and path.suffix != ".py")])
    assert {name for name, _ in shell} >= {
        ".github/workflows/deploy.yml", ".github/workflows/deploy-production.yml",
        ".github/workflows/deploy-azure-control-plane.yml", ".github/workflows/deploy-growth-sync.yml",
        "scripts/deploy/aws_ecs_control_plane.sh", "scripts/deploy/settle_drain_worker.sh"}
    for name, command in shell:
        assert "--limit 1" not in command, name
        assert ",event" in command, name
        if name.endswith(".sh"):
            continue  # the script filters the listing itself; checked below
        assert 'select(.event == "push" or .event == "workflow_dispatch")' in command, name
    drain = (ROOT / "scripts/deploy/settle_drain_worker.sh").read_text()
    assert 'runs=[run for run in json.load(sys.stdin) if run.get("event") in ("push", "workflow_dispatch")]' in drain
    aws = (ROOT / "scripts/deploy/aws_ecs_control_plane.sh").read_text()
    assert """--jq '[.[] | select(.event == "push" or .event == "workflow_dispatch")][0].conclusion'""" in aws
    python = python_ci_lookups([path for path in scripts if path.suffix == ".py"])
    assert {name for name, _ in python} >= {"scripts/deploy/release_train.py", "scripts/deploy/superseded_deploy.py"}
    for name, values in python:
        # Either the listing asks for push runs, or it fetches each run's
        # event for the caller to filter (release_train's ci(), tested in
        # tests/test_release_train.py).
        by_event = ("--event", "push") in list(zip(values, values[1:], strict=False))
        fields = values[values.index("--json") + 1] if "--json" in values else ""
        assert by_event or (isinstance(fields, str) and "event" in fields.split(",")), (name, values)
        assert "--limit" not in values or by_event, (name, values)
