"""The deploy workflow's CI gate, executed against a fake clock and a fake CI API.

The gate polls ci.yml for the pushed commit. A saturated runner queue has held
CI for hours (2026-10-19), so the gate waits as long as a CI run exists and is
queued or running, up to its run deadline, and still refuses at once on a red
CI. A CI run that never appears keeps the original 45-minute bound. Only a
push or dispatch run counts: a pull_request run checks a merge commit and
skips the jobs its pull request did not change.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/deploy.yml"
STEP_SECONDS = 300  # every `date +%s` call advances the fake clock by 5 minutes
BASE = 1_800_000_000

pytestmark = pytest.mark.skipif(shutil.which("jq") is None or shutil.which("bash") is None,
                                reason="the gate script needs bash and jq")


def _gate_job() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["gate-on-ci"]


def _gate_script(hotfix: str = "false") -> str:
    steps = [s for s in _gate_job()["steps"] if s.get("name") == "Gate on CI green (same commit)"]
    assert len(steps) == 1
    script = steps[0]["run"].replace("${{ inputs.hotfix }}", hotfix)
    script = script.replace("${{ inputs.track_main }}", "false")
    assert "${{" not in script
    return script


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, responses: list[object], *, hotfix: str = "false") -> tuple[subprocess.CompletedProcess[str], int, int]:
    """Run the gate; `responses` are the successive `gh run list --jq` answers (last one repeats).

    None prints `null` (jq's empty-list answer); "" prints nothing (gh's own formatter for an empty list);
    "ERROR" exits 1 with an HTTP error on stderr, as a failed API call does; {"runs": [...]} is the
    listing itself, which the gate's own --jq filter (run with the real jq) reduces.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "responses.json").write_text(json.dumps(responses))
    (tmp_path / "gh_calls").write_text("0")
    (tmp_path / "date_calls").write_text("0")
    _executable(bin_dir / "gh", f"""#!/usr/bin/env python3
import json, sys
state = {str(tmp_path)!r}
calls = int(open(state + "/gh_calls").read())
open(state + "/gh_calls", "w").write(str(calls + 1))
assert sys.argv[1:3] == ["run", "list"], sys.argv
assert "--workflow" in sys.argv and sys.argv[sys.argv.index("--workflow") + 1] == "ci.yml"
assert sys.argv[sys.argv.index("--commit") + 1] == "abc123"
fields = sys.argv[sys.argv.index("--json") + 1].split(",")
assert "event" in fields, fields
responses = json.load(open(state + "/responses.json"))
answer = responses[min(calls, len(responses) - 1)]
if answer == "ERROR":
    print("HTTP 503: Service Unavailable", file=sys.stderr)
    sys.exit(1)
if isinstance(answer, dict) and "runs" in answer:
    import subprocess
    sys.exit(subprocess.run(["jq", "-c", sys.argv[sys.argv.index("--jq") + 1]],
                            input=json.dumps(answer["runs"]), text=True).returncode)
print("null" if answer is None else "" if answer == "" else json.dumps(answer))
""")
    _executable(bin_dir / "date", f"""#!/usr/bin/env python3
import subprocess, sys
state = {str(tmp_path)!r}
if sys.argv[1:] == ["+%s"]:
    calls = int(open(state + "/date_calls").read())
    open(state + "/date_calls", "w").write(str(calls + 1))
    print({BASE} + calls * {STEP_SECONDS})
else:
    sys.exit(subprocess.call(["/bin/date", *sys.argv[1:]]))
""")
    _executable(bin_dir / "sleep", "#!/bin/sh\nexit 0\n")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", GITHUB_SHA="abc123",
               GITHUB_REPOSITORY="Lore-Hex/quill-router")
    script = tmp_path / "gate.sh"
    script.write_text(_gate_script(hotfix))
    bash = shutil.which("bash")
    assert bash is not None
    result = subprocess.run(  # noqa: S603 - the workflow's own gate script, fake gh/date/sleep on PATH
        [bash, str(script)], env=env, capture_output=True, text=True, timeout=120)
    elapsed = (int((tmp_path / "date_calls").read_text()) - 1) * STEP_SECONDS
    return result, int((tmp_path / "gh_calls").read_text()), elapsed


QUEUED = {"databaseId": 7, "status": "queued", "conclusion": ""}
RUNNING = {"databaseId": 7, "status": "in_progress", "conclusion": ""}
GREEN = {"databaseId": 7, "status": "completed", "conclusion": "success"}
RED = {"databaseId": 7, "status": "completed", "conclusion": "failure"}


def test_ci_queued_for_hours_then_green_proceeds(tmp_path: Path) -> None:
    # Four hours of queue and run time: the old 45-minute gate refused this deploy.
    result, calls, elapsed = _run(tmp_path, [QUEUED] * 30 + [RUNNING] * 18 + [GREEN])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "CI is green" in result.stdout
    assert calls == 49
    assert 4 * 3600 <= elapsed < 330 * 60


def test_red_ci_refuses_at_once(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, [QUEUED, RUNNING, RED])
    assert result.returncode == 1
    assert "refusing to deploy a red commit" in result.stdout
    assert calls == 3


def test_cancelled_ci_refuses(tmp_path: Path) -> None:
    result, _, _ = _run(tmp_path, [RUNNING, {"databaseId": 7, "status": "completed", "conclusion": "cancelled"}])
    assert result.returncode == 1
    assert "concluded 'cancelled'" in result.stdout


def test_missing_ci_run_keeps_the_45_minute_bound(tmp_path: Path) -> None:
    result, _, elapsed = _run(tmp_path, [None])
    assert result.returncode == 1
    assert "timed out after 45 min: no CI run found for abc123" in result.stdout
    assert 45 * 60 <= elapsed < 60 * 60


def test_ci_that_never_finishes_stops_at_the_run_deadline(tmp_path: Path) -> None:
    result, _, elapsed = _run(tmp_path, [RUNNING])
    assert result.returncode == 1
    assert "timed out after 330 min waiting for CI on abc123" in result.stdout
    assert 330 * 60 <= elapsed < 340 * 60


def test_a_late_appearing_ci_run_is_then_waited_for(tmp_path: Path) -> None:
    result, _, _ = _run(tmp_path, [None, ""] * 2 + [QUEUED] * 20 + [GREEN])
    assert result.returncode == 0, result.stdout
    assert "CI is green" in result.stdout


def test_a_lookup_error_after_the_run_was_seen_is_retried(tmp_path: Path) -> None:
    # Ten running answers pass the 45-minute mark; failed or empty lookups then must not read as "no run".
    result, _, _ = _run(tmp_path, [RUNNING] * 10 + ["ERROR", "", None, "ERROR"] + [GREEN])
    assert result.returncode == 0, result.stdout
    assert "CI run lookup failed for abc123; retrying" in result.stdout
    assert "CI is green" in result.stdout


def test_an_api_error_before_the_run_appears_is_retried(tmp_path: Path) -> None:
    result, _, _ = _run(tmp_path, ["ERROR", "ERROR", QUEUED, GREEN])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "no CI run found yet for abc123" in result.stdout


def test_hotfix_skips_the_gate(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, [RED], hotfix="true")
    assert result.returncode == 0
    assert "HOTFIX MODE" in result.stdout
    assert calls == 0


def test_the_job_limit_covers_the_run_deadline() -> None:
    job = _gate_job()
    # GitHub caps a hosted job at 360 minutes; the poll's 330-minute run deadline must fit inside the job limit.
    assert 330 < job["timeout-minutes"] <= 360


PUSH_GREEN = {**GREEN, "event": "push"}
PUSH_RUNNING = {**RUNNING, "event": "push"}
PR_GREEN = {"databaseId": 9, "status": "completed", "conclusion": "success", "event": "pull_request"}
PR_RED = {"databaseId": 9, "status": "completed", "conclusion": "failure", "event": "pull_request"}


def test_a_pull_request_run_never_serves_as_the_gate(tmp_path: Path) -> None:
    # A green pull_request run for the commit, and no push or dispatch run:
    # the gate keeps looking and refuses at the 45-minute bound.
    result, _, _ = _run(tmp_path, [{"runs": [PR_GREEN]}])
    assert result.returncode == 1
    assert "timed out after 45 min: no CI run found for abc123" in result.stdout
    assert "CI is green" not in result.stdout


@pytest.mark.parametrize(("name", "answers"), [
    # A newer green pull_request run does not end the wait for the push run.
    ("newer pull request", [{"runs": [PR_GREEN, PUSH_RUNNING]}] * 3 + [{"runs": [PR_GREEN, PUSH_GREEN]}]),
    # A red pull_request run does not refuse a green push run.
    ("red pull request", [{"runs": [PR_RED, PUSH_GREEN]}]),
    # The price refresh's commits carry only their dispatch run.
    ("dispatch", [{"runs": [{**GREEN, "event": "workflow_dispatch"}]}]),
])
def test_the_push_or_dispatch_run_decides(tmp_path: Path, name: str, answers: list[object]) -> None:
    result, _, _ = _run(tmp_path, answers)
    assert result.returncode == 0, name + result.stdout + result.stderr
    assert "ci run 7:" in result.stdout and "ci run 9:" not in result.stdout, name
    assert "CI is green" in result.stdout, name


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
