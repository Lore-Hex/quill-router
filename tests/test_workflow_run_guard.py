"""Every workflow_run-triggered job must refuse runs from forks.

`workflow_run` matches the triggering workflow by its `name`, and on a
pull request GitHub runs the pull request's own workflow files, so a fork
can add a workflow named like any of ours. Its completion then starts our
workflow_run workflows in this repository's context, with its secrets and
token, and `head_sha` names the fork's commit. Each job of a workflow_run
workflow therefore requires the triggering run's head repository to be
this one and its event to be one the real triggering workflow uses.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SAME_REPO = "github.event.workflow_run.head_repository.full_name == github.repository"


def _on(doc: dict) -> dict:
    on = doc.get(True, doc.get("on"))
    return on if isinstance(on, dict) else {}


def workflow_run_workflows() -> list[Path]:
    found = []
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        if "workflow_run" in _on(doc):
            found.append(path)
    return found


def unguarded_jobs(doc: dict) -> list[str]:
    bad = []
    for name, job in doc["jobs"].items():
        condition = " ".join(str(job.get("if", "")).split())
        if SAME_REPO not in condition or "github.event.workflow_run.event ==" not in condition:
            bad.append(name)
    return bad


def test_there_are_workflow_run_workflows_to_check() -> None:
    assert len(workflow_run_workflows()) >= 5


@pytest.mark.parametrize("path", workflow_run_workflows(), ids=lambda p: p.name)
def test_every_job_refuses_fork_runs(path: Path) -> None:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert unguarded_jobs(doc) == []


@pytest.mark.parametrize("path", workflow_run_workflows(), ids=lambda p: p.name)
def test_other_triggers_are_not_blocked_by_the_guard(path: Path) -> None:
    # On a schedule or dispatch the workflow_run fields are empty, so a guard
    # that is not scoped to workflow_run events would skip those runs.
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if set(_on(doc)) == {"workflow_run"}:
        return
    for name, job in doc["jobs"].items():
        condition = " ".join(str(job.get("if", "")).split())
        assert f"github.event_name != 'workflow_run' || ({SAME_REPO}" in condition, name


def test_a_job_without_the_guard_is_reported() -> None:
    doc = {"jobs": {"smoke": {"if": "github.event.workflow_run.conclusion == 'success'"}}}
    assert unguarded_jobs(doc) == ["smoke"]
