"""ci.yml's base-ci job, run verbatim with a fake gh.

When the scope job takes the data-only skips over a base whose own push run
is still running (a price refresh dispatch), base-ci holds the run until
that push run concludes success, and fails it otherwise.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
BASE = "b" * 40


def base_ci_script() -> str:
    job = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["base-ci"]
    (step,) = job["steps"]
    return step["run"]


def run_wait(tmp_path: Path, answers: list[list[dict[str, object]]]) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run the step; each gh call answers with the next listing (the last repeats)."""
    bash, jq = shutil.which("bash"), shutil.which("jq")
    if not bash or not jq:
        pytest.skip("base-ci needs bash and jq")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "jq").symlink_to(jq)
    for tool in ("seq",):
        found = shutil.which(tool)
        assert found
        (bin_dir / tool).symlink_to(found)
    (tmp_path / "answers.json").write_text(json.dumps(answers))
    (bin_dir / "gh").write_text(f"""#!{bash}
set -euo pipefail
[ "$1 $2" = "api repos/fixture/quill-router/actions/workflows/ci.yml/runs?head_sha={BASE}&per_page=100" ] || {{ echo "unexpected: $*" >&2; exit 1; }}
filter=$4
n=0; [ -f "{tmp_path}/calls" ] && read -r n < "{tmp_path}/calls"
echo $((n + 1)) > "{tmp_path}/calls"
jq -c --argjson i "$n" '.[([$i, length - 1] | min)] | {{workflow_runs: .}}' "{tmp_path}/answers.json" | jq -c "$filter"
""")
    (bin_dir / "sleep").write_text(f"#!{bash}\nexit 0\n")
    for name in ("gh", "sleep"):
        (bin_dir / name).chmod(0o700)
    script = tmp_path / "wait.sh"
    script.write_text(base_ci_script())
    result = subprocess.run(  # noqa: S603 -- the workflow step against a fake gh
        [bash, "--noprofile", "--norc", str(script)], capture_output=True, text=True, timeout=60,
        env={"PATH": str(bin_dir), "BASE": BASE, "GITHUB_REPOSITORY": "fixture/quill-router"},
    )
    calls = int((tmp_path / "calls").read_text()) if (tmp_path / "calls").exists() else 0
    return result, calls


def run(status: str, conclusion: str | None = None, event: str = "push") -> dict[str, object]:
    return {"status": status, "conclusion": conclusion, "event": event}


def test_waits_for_the_running_base_run_to_succeed(tmp_path: Path) -> None:
    result, calls = run_wait(tmp_path, [[run("queued")], [run("in_progress")], [run("completed", "success")]])
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls == 3


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_fails_when_the_base_run_concludes_without_success(tmp_path: Path, conclusion: str) -> None:
    result, _ = run_wait(tmp_path, [[run("in_progress")], [run("completed", conclusion)]])
    assert result.returncode == 1
    assert "no ci.yml run of base" in result.stdout


def test_a_dispatch_success_counts_and_pull_request_runs_do_not(tmp_path: Path) -> None:
    result, _ = run_wait(tmp_path, [[run("completed", "failure"), run("completed", "success", "workflow_dispatch")]])
    assert result.returncode == 0
    second = tmp_path / "pull-request-only"
    second.mkdir()
    result, _ = run_wait(second, [[run("completed", "success", "pull_request")]])
    assert result.returncode == 1


def test_gives_up_after_its_bounded_wait(tmp_path: Path) -> None:
    result, calls = run_wait(tmp_path, [[run("in_progress")]])
    assert result.returncode == 1
    assert "did not conclude within 75 minutes" in result.stdout
    assert calls == 300
