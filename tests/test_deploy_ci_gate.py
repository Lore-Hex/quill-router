"""The deploy workflow's CI gate, executed against a fake clock and a fake CI API.

The gate polls ci.yml for the pushed commit. A saturated runner queue has held
CI for hours (2026-10-19), so the gate waits as long as a CI run exists and is
queued or running, up to its run deadline, and still refuses at once on a red
CI. A CI run that never appears keeps the original 45-minute bound.
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
    assert "${{" not in script
    return script


def _executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _run(tmp_path: Path, responses: list[object], *, hotfix: str = "false") -> tuple[subprocess.CompletedProcess[str], int, int]:
    """Run the gate; `responses` are the successive `gh run list --jq '.[0]'` answers (last one repeats).

    None prints `null` (no run listed yet); "" prints nothing (a failed API call, swallowed by `|| true`).
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
responses = json.load(open(state + "/responses.json"))
answer = responses[min(calls, len(responses) - 1)]
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


def test_hotfix_skips_the_gate(tmp_path: Path) -> None:
    result, calls, _ = _run(tmp_path, [RED], hotfix="true")
    assert result.returncode == 0
    assert "HOTFIX MODE" in result.stdout
    assert calls == 0


def test_the_job_limit_covers_the_run_deadline() -> None:
    job = _gate_job()
    # GitHub caps a hosted job at 360 minutes; the poll's 330-minute run deadline must fit inside the job limit.
    assert 330 < job["timeout-minutes"] <= 360
