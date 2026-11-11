"""A published price snapshot rides the next rollout; one that waited too long gets its own."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from scripts import price_refresh_alerts
from scripts.deploy import price_snapshot_deploy as check

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/refresh-prices.yml"
STEP_NAME = "Deploy a price snapshot no rollout has carried"
# Pinned by tests/test_refresh_workflow_dispatch.py too: deploy.yml follows main only with track_main.
DEPLOY_DISPATCH = 'gh workflow run deploy.yml --ref main --repo "${GITHUB_REPOSITORY}" -f track_main=true'
DISPATCH_ARGV = ["workflow", "run", "deploy.yml", "--ref", "main", "--repo", "fixture/quill-router",
                 "-f", "track_main=true"]

REPO = "fixture/quill-router"
NOW = 1_794_000_000.0
LIMIT = 4 * 3600
DELIVERED = "d" * 40
MAIN = "a" * 40
SNAPSHOT = "5" * 40
RUN_HEAD = "b" * 40
DELIVERY = ("deploy", "rollout-secondaries", "public-surface-companion", "verify-cloud-complete", "finalize-cloud")
DELIVERED_JOBS = {"jobs": [{"name": f"rollout / {name}", "conclusion": "success"} for name in DELIVERY]}
SKIPPED_ROLLOUT = {"jobs": [{"name": "gate-on-ci", "conclusion": "success"},
                            {"name": "rollout / confirm-current-main", "conclusion": "success"},
                            *[{"name": f"rollout / {name}", "conclusion": "skipped"} for name in DELIVERY]]}

# The gh calls, written out: the tests pin what is asked, not only how often.
SUCCESSFUL = ["run", "list", "--repo", REPO, "--workflow", "deploy.yml", "--branch", "main",
              "--status", "success", "--limit", "20", "--json", "databaseId,headSha"]
HEAD = ["api", f"repos/{REPO}/commits/main"]
ACTIVE = ["run", "list", "--repo", REPO, "--workflow", "deploy.yml", "--branch", "main",
          "--limit", "100", "--json", "databaseId,status,headSha,headBranch"]


def view(run_id: int) -> list[str]:
    return ["run", "view", str(run_id), "--repo", REPO, "--json", "jobs"]


def compare(base: str, head: str) -> list[str]:
    return ["api", f"repos/{REPO}/compare/{base}...{head}"]


def of_head(sha: str) -> list[str]:
    return ["run", "list", "--repo", REPO, "--workflow", "deploy.yml", "--commit", sha,
            "--limit", "20", "--json", "databaseId,status,conclusion"]


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def commit(sha: str, subject: str, age: float, *, now: float = NOW) -> dict:
    return {"sha": sha, "commit": {"message": f"{subject}\n\nPrice changes:\n(none)\n",
                                   "committer": {"date": iso(now - age)}}}


def snapshot(age: float, sha: str = SNAPSHOT, *, now: float = NOW) -> dict:
    return commit(sha, "[bot] hourly price refresh: 2026-11-06T17:01Z", age, now=now)


CODE = commit("c" * 40, "Shorten the settle budget", 9 * 3600)


def key(args: list[str]) -> str:
    return " ".join(args)


def world(
    commits: list[dict] | None = None, *, head_runs: list[dict] | None = None,
    active: list[dict] | None = None, lineage: dict[str, str] | None = None,
) -> dict[str, object]:
    """gh's answers: release 31 delivered DELIVERED, and main has `commits` beyond it."""

    commits = [CODE, snapshot(5 * 3600)] if commits is None else commits
    answers: dict[str, object] = {
        key(SUCCESSFUL): [{"databaseId": 31, "headSha": DELIVERED}],
        key(view(31)): DELIVERED_JOBS,
        key(HEAD): {"sha": MAIN},
        key(compare(DELIVERED, MAIN)): {"status": "ahead", "total_commits": len(commits), "commits": commits},
        key(of_head(MAIN)): head_runs or [],
        key(ACTIVE): active or [],
    }
    for run_head, status in (lineage or {}).items():
        answers[key(compare(SNAPSHOT, run_head))] = {"status": status}
    return answers


def active_run(head: str = RUN_HEAD, run_id: int = 77, status: str = "in_progress", branch: str = "main") -> dict:
    return {"databaseId": run_id, "status": status, "headSha": head, "headBranch": branch}


@dataclass
class Outcome:
    code: int
    stdout: str
    stderr: str
    calls: list[list[str]]
    sleeps: list[float] = field(default_factory=list)


@pytest.fixture
def decide(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]):
    """Run main() against a table of gh answers; a call outside the table fails the test."""

    def run(answers: dict[str, object], *, failures: dict[str, int] | None = None) -> Outcome:
        remaining = dict(failures or {})
        calls: list[list[str]] = []
        sleeps: list[float] = []

        def fake_gh_json(args: list[str], deadline: float | None = None) -> object | None:
            calls.append(list(args))
            asked = key(args)
            if remaining.get(asked, 0) > 0:
                remaining[asked] -= 1
                return None
            assert asked in answers, f"unexpected gh call: {asked}"
            return copy.deepcopy(answers[asked])

        monkeypatch.setattr(check, "_gh_json", fake_gh_json)
        monkeypatch.setattr(check, "_now", lambda: NOW)
        monkeypatch.setattr(check, "_sleep", sleeps.append)
        code = check.main(["--repo", REPO])
        captured = capsys.readouterr()
        return Outcome(code, captured.out, captured.err, calls, sleeps)

    return run


TO_COMPARE = [SUCCESSFUL, view(31), HEAD, compare(DELIVERED, MAIN)]
TO_ACTIVE = [*TO_COMPARE, of_head(MAIN), ACTIVE]


def assert_waits(outcome: Outcome, calls: list[list[str]]) -> None:
    assert (outcome.code, outcome.stdout) == (0, "wait\n"), outcome.stderr
    assert outcome.calls == calls
    assert "price_snapshot_deploy: wait: " in outcome.stderr


def assert_dispatches(outcome: Outcome, calls: list[list[str]]) -> None:
    assert (outcome.code, outcome.stdout) == (0, "dispatch\n"), outcome.stderr
    assert outcome.calls == calls
    assert "price_snapshot_deploy: dispatch: " in outcome.stderr


def assert_unknown(outcome: Outcome) -> None:
    assert outcome.code == 1, outcome.stderr
    assert outcome.stdout == "", "an unknown answer must put nothing on stdout"
    assert "price_snapshot_deploy: unknown: " in outcome.stderr


def test_the_limit_is_four_hours_and_the_helpers_are_the_superseded_check_s() -> None:
    from scripts.deploy import superseded_deploy

    assert check.SNAPSHOT_MAX_WAIT_SECONDS == LIMIT
    assert (check.GH_ATTEMPTS, check.GH_RETRY_SECONDS) == (3, 5)
    assert check.delivered is superseded_deploy.delivered
    assert check._epoch is superseded_deploy._epoch
    assert check._gh_json is superseded_deploy._gh_json
    assert (check.DEPLOY_WORKFLOW, check.MAIN_BRANCH) == ("deploy.yml", "main")


def test_waits_when_main_is_the_delivered_release(decide) -> None:
    answers = {
        key(SUCCESSFUL): [{"databaseId": 31, "headSha": DELIVERED}],
        key(view(31)): DELIVERED_JOBS,
        key(HEAD): {"sha": DELIVERED},
        key(compare(DELIVERED, DELIVERED)): {"status": "identical", "total_commits": 0, "commits": []},
    }
    assert_waits(decide(answers), [SUCCESSFUL, view(31), HEAD, compare(DELIVERED, DELIVERED)])


def test_waits_when_no_undelivered_commit_is_a_snapshot(decide) -> None:
    commits = [
        CODE,
        # Only the subject's start names a snapshot: not a body line, not a revert of one.
        commit("e" * 40, "Explain the refresh\n\n[bot] hourly price refresh: 2026-11-06T17:01Z", 8 * 3600),
        commit("f" * 40, 'Revert "[bot] hourly price refresh: 2026-11-06T17:01Z"', 7 * 3600),
    ]
    outcome = decide(world(commits))
    assert_waits(outcome, TO_COMPARE)
    assert "none of the 3 commit(s)" in outcome.stderr


def test_waits_for_a_young_snapshot(decide) -> None:
    outcome = decide(world([CODE, snapshot(3600)]))
    assert_waits(outcome, TO_COMPARE)
    assert f"snapshot {SNAPSHOT[:9]} has waited 1.0 h (limit 4 h)" in outcome.stderr


def test_the_limit_itself_dispatches_and_one_second_less_waits(decide) -> None:
    assert_dispatches(decide(world([CODE, snapshot(LIMIT)])), TO_ACTIVE)
    assert_waits(decide(world([CODE, snapshot(LIMIT - 1)])), TO_COMPARE)


def test_dispatches_for_an_old_snapshot_no_run_carries(decide) -> None:
    outcome = decide(world())
    assert_dispatches(outcome, TO_ACTIVE)
    assert f"snapshot {SNAPSHOT[:9]} has waited 5.0 h" in outcome.stderr


@pytest.mark.parametrize("status, conclusion", [
    ("queued", None), ("in_progress", None), ("completed", "failure"), ("completed", "cancelled"),
    ("completed", "success"),
])
def test_waits_when_main_s_head_already_has_a_deploy_run(decide, status: str, conclusion: str | None) -> None:
    # In flight it is the carrier; completed, a deploy of this head was tried
    # and a person decides. Either way: one automatic dispatch per main head.
    head_runs = [{"databaseId": 88, "status": status, "conclusion": conclusion}]
    outcome = decide(world(head_runs=head_runs))
    assert_waits(outcome, [*TO_COMPARE, of_head(MAIN)])
    assert f"main's head {MAIN[:9]} already has a deploy run (88 {status} {conclusion or '-'})" in outcome.stderr


@pytest.mark.parametrize("status", ["ahead", "identical"])
def test_waits_when_an_active_run_carries_the_snapshot(decide, status: str) -> None:
    outcome = decide(world(active=[active_run()], lineage={RUN_HEAD: status}))
    assert_waits(outcome, [*TO_ACTIVE, compare(SNAPSHOT, RUN_HEAD)])
    assert f"an active deploy run of {RUN_HEAD[:9]} carries it" in outcome.stderr


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_dispatches_when_the_active_run_is_of_a_commit_without_the_snapshot(decide, status: str) -> None:
    outcome = decide(world(active=[active_run()], lineage={RUN_HEAD: status}))
    assert_dispatches(outcome, [*TO_ACTIVE, compare(SNAPSHOT, RUN_HEAD)])


def test_only_runs_on_main_that_have_not_completed_are_carriers_and_each_commit_is_asked_once(decide) -> None:
    other = "9" * 40
    active = [
        active_run("1" * 40, 70, status="completed"),  # done: carries nothing any more
        active_run("2" * 40, 71, branch="hotfix"),  # not a rollout of main
        active_run(RUN_HEAD, 72, status="queued"),
        active_run(RUN_HEAD, 73, status="in_progress"),  # the same commit again
        active_run(other, 74, status="waiting"),
    ]
    outcome = decide(world(active=active, lineage={RUN_HEAD: "behind", other: "ahead"}))
    assert_waits(outcome, [*TO_ACTIVE, compare(SNAPSHOT, RUN_HEAD), compare(SNAPSHOT, other)])
    outcome = decide(world(active=active, lineage={RUN_HEAD: "behind", other: "diverged"}))
    assert_dispatches(outcome, [*TO_ACTIVE, compare(SNAPSHOT, RUN_HEAD), compare(SNAPSHOT, other)])


def test_the_older_of_two_waiting_snapshots_decides(decide) -> None:
    older = "0" * 40
    # Listed second, so position cannot stand in for the committer date.
    commits = [snapshot(3600), snapshot(5 * 3600, older), CODE]
    answers = world(commits, active=[active_run()])
    answers[key(compare(older, RUN_HEAD))] = {"status": "behind"}
    outcome = decide(answers)
    assert_dispatches(outcome, [*TO_ACTIVE, compare(older, RUN_HEAD)])
    assert f"snapshot {older[:9]} has waited 5.0 h" in outcome.stderr
    # Both young: the older one is still inside the limit.
    assert_waits(decide(world([snapshot(3600), snapshot(LIMIT - 1, older), CODE])), TO_COMPARE)


def test_a_successful_run_whose_rollout_was_skipped_is_not_the_delivered_release(decide) -> None:
    skipped = "7" * 40
    answers = world()
    answers[key(SUCCESSFUL)] = [{"databaseId": 41, "headSha": skipped}, {"databaseId": 31, "headSha": DELIVERED},
                                {"databaseId": 21, "headSha": "8" * 40}]
    answers[key(view(41))] = SKIPPED_ROLLOUT
    outcome = decide(answers)
    # The compare starts at run 31's commit, and run 21 is never read.
    assert_dispatches(outcome, [SUCCESSFUL, view(41), view(31), HEAD, compare(DELIVERED, MAIN), of_head(MAIN), ACTIVE])


ALL_CALLS = [*TO_ACTIVE, compare(SNAPSHOT, RUN_HEAD)]
CALL_IDS = ["successful-runs", "run-view", "main-head", "compare", "runs-of-head", "active-runs", "lineage"]


def reaches_every_call() -> dict[str, object]:
    return world(active=[active_run()], lineage={RUN_HEAD: "behind"})


@pytest.mark.parametrize("failing", range(len(ALL_CALLS)), ids=CALL_IDS)
def test_each_gh_call_that_never_answers_is_unknown(decide, failing: int) -> None:
    assert_dispatches(decide(reaches_every_call()), ALL_CALLS)
    outcome = decide(reaches_every_call(), failures={key(ALL_CALLS[failing]): 3})
    assert_unknown(outcome)
    # Asked three times, five seconds apart, and nothing after it.
    assert outcome.calls == [*ALL_CALLS[:failing], *[ALL_CALLS[failing]] * 3]
    assert outcome.sleeps == [5, 5]
    assert "gave no answer in 3 attempts" in outcome.stderr


@pytest.mark.parametrize("failing", range(len(ALL_CALLS)), ids=CALL_IDS)
def test_a_gh_call_that_answers_on_its_third_attempt_is_used(decide, failing: int) -> None:
    outcome = decide(reaches_every_call(), failures={key(ALL_CALLS[failing]): 2})
    assert_dispatches(outcome, [*ALL_CALLS[:failing], *[ALL_CALLS[failing]] * 3, *ALL_CALLS[failing + 1:]])
    assert outcome.sleeps == [5, 5]


def test_a_gh_call_that_answers_at_once_never_pauses(decide) -> None:
    assert decide(reaches_every_call()).sleeps == []


def changed(call: list[str], answer: object) -> dict[str, object]:
    answers = reaches_every_call()
    assert key(call) in answers
    answers[key(call)] = answer
    return answers


def compared(**changes: object) -> dict[str, object]:
    answer = {"status": "ahead", "total_commits": 2, "commits": [CODE, snapshot(5 * 3600)], **changes}
    return changed(compare(DELIVERED, MAIN), answer)


def with_snapshot(entry: dict) -> dict[str, object]:
    return compared(commits=[CODE, entry])


def snapshot_with(**commit_fields: object) -> dict:
    entry = snapshot(5 * 3600)
    entry["commit"].update(commit_fields)
    return entry


UNKNOWNS: dict[str, dict[str, object]] = {
    "successful runs are not a list": changed(SUCCESSFUL, {"message": "Not Found"}),
    "no successful run": changed(SUCCESSFUL, []),
    "no successful run delivered": changed(view(31), SKIPPED_ROLLOUT),
    "a successful run without an id": changed(SUCCESSFUL, [{"headSha": DELIVERED}]),
    "a successful run that is not an object": changed(SUCCESSFUL, [31]),
    "jobs are not a list": changed(view(31), {"jobs": None}),
    "run view is not an object": changed(view(31), []),
    "delivered sha is short": changed(SUCCESSFUL, [{"databaseId": 31, "headSha": DELIVERED[:9]}]),
    "delivered sha is upper case": changed(SUCCESSFUL, [{"databaseId": 31, "headSha": "D" * 40}]),
    "delivered sha is 41 digits": changed(SUCCESSFUL, [{"databaseId": 31, "headSha": "d" * 41}]),
    "delivered sha has a trailing newline": changed(SUCCESSFUL, [{"databaseId": 31, "headSha": DELIVERED + "\n"}]),
    "delivered sha is missing": changed(SUCCESSFUL, [{"databaseId": 31}]),
    "main's head is not an object": changed(HEAD, [MAIN]),
    "main's head has no sha": changed(HEAD, {"commit": {}}),
    "main's head sha is a branch name": changed(HEAD, {"sha": "main"}),
    "the compare is not an object": changed(compare(DELIVERED, MAIN), []),
    "main is behind the delivered release": compared(status="behind"),
    "main diverged from the delivered release": compared(status="diverged"),
    "the compare has no status": compared(status=None),
    "the commit list is truncated": compared(total_commits=3),
    "the commit list is longer than the count": compared(total_commits=1),
    "the commit count is missing": compared(total_commits=None),
    "the commit list is missing": compared(commits=None),
    "ahead without commits": compared(total_commits=0, commits=[]),
    "a commit is not an object": compared(commits=[CODE, "5" * 40]),
    "a commit has no message": compared(commits=[{"sha": "c" * 40, "commit": {"committer": {"date": iso(NOW)}}},
                                                 snapshot(5 * 3600)]),
    "a commit has no commit object": compared(commits=[{"sha": "c" * 40}, snapshot(5 * 3600)]),
    "the snapshot's date is not a time": with_snapshot(snapshot_with(committer={"date": "yesterday"})),
    "the snapshot's date is null": with_snapshot(snapshot_with(committer={"date": None})),
    "the snapshot has no committer": with_snapshot(snapshot_with(committer=None)),
    "the snapshot's sha is short": with_snapshot({**snapshot(5 * 3600), "sha": SNAPSHOT[:12]}),
    "the snapshot's sha is missing": with_snapshot({"commit": snapshot(5 * 3600)["commit"]}),
    "runs of main's head are not a list": changed(of_head(MAIN), {"message": "Not Found"}),
    "a run of main's head is not an object": changed(of_head(MAIN), [88]),
    "active runs are not a list": changed(ACTIVE, {}),
    "an active run is not an object": changed(ACTIVE, [77]),
    "an active run has no status": changed(ACTIVE, [{"databaseId": 77, "headSha": RUN_HEAD, "headBranch": "main"}]),
    "an active run's sha is short": changed(ACTIVE, [active_run(RUN_HEAD[:7])]),
    "the lineage compare is not an object": changed(compare(SNAPSHOT, RUN_HEAD), []),
    "the lineage compare has no status": changed(compare(SNAPSHOT, RUN_HEAD), {}),
    "the lineage compare has a new status": changed(compare(SNAPSHOT, RUN_HEAD), {"status": "unrelated"}),
}


@pytest.mark.parametrize("case", sorted(UNKNOWNS))
def test_an_answer_the_rule_cannot_read_is_unknown(decide, case: str) -> None:
    outcome = decide(UNKNOWNS[case])
    assert_unknown(outcome)
    assert outcome.sleeps == [], "an answer that arrived is not asked again"


def test_the_unknown_table_differs_from_a_decision_by_one_answer(decide) -> None:
    # The base every row of UNKNOWNS changes decides: each row is unknown for its own change alone.
    assert_dispatches(decide(reaches_every_call()), ALL_CALLS)
    assert_dispatches(decide(compared()), ALL_CALLS)
    assert_dispatches(decide(with_snapshot(snapshot_with())), ALL_CALLS)


def test_a_malformed_commit_that_is_not_a_snapshot_still_needs_only_its_message(decide) -> None:
    # Ids and dates are read from snapshots alone; other commits' fields decide nothing.
    code = {"sha": "short", "commit": {"message": "Shorten the settle budget", "committer": None}}
    assert_dispatches(decide(compared(commits=[code, snapshot(5 * 3600)])), ALL_CALLS)


def _steps() -> list[dict]:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["refresh"]["steps"]


def _publication_script() -> str:
    (step,) = [step for step in _steps() if step.get("id") == "publication"]
    return step["run"]


def test_the_subject_prefix_is_the_one_the_refresh_commits_with() -> None:
    script = _publication_script()
    lines = [line.strip() for line in script.splitlines()]
    message = "/tmp/commit-msg.txt"  # noqa: S108 - the workflow's own scratch path, only read as text
    assert lines.count(f"git commit -F {message}") == 1
    assert sum(line.startswith("git commit ") for line in lines) == 1, "every snapshot commit uses that message"
    end = lines.index(f"}} > {message}")
    start = max(i for i, line in enumerate(lines[:end]) if line == "{")
    # The block's first line is the subject.
    assert lines[start + 1] == f'echo "{check.SNAPSHOT_SUBJECT_PREFIX}: ${{TIMESTAMP}}"'
    # A snapshot carried onto a newer main keeps the whole message.
    assert 'git show -s --format=%B HEAD | git commit-tree "${tree}" -p "${target}"' in script


def test_the_deploy_step_is_the_refresh_job_s_last_and_runs_after_failures() -> None:
    steps = _steps()
    step = steps[-1]
    assert step["name"] == STEP_NAME
    assert [s.get("name") for s in steps].count(STEP_NAME) == 1
    # Not `always()`: a cancelled run dispatches nothing. Not the default
    # either: an earlier failed step must not stop the check.
    assert step["if"] == "${{ !cancelled() }}"
    assert step["env"] == {"GH_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}
    assert set(step) == {"name", "if", "env", "run"}
    script = step["run"]
    assert script.startswith("set -euo pipefail\n")
    assert 'decision=$(python3 -m scripts.deploy.price_snapshot_deploy --repo "${GITHUB_REPOSITORY}")' in script
    assert script.count(DEPLOY_DISPATCH) == 1
    workflow = WORKFLOW.read_text()
    assert workflow.count("gh workflow run deploy.yml") == 1, "only the last step dispatches deploy.yml"
    assert "actions: write" in workflow
    assert "gh workflow run deploy.yml" not in _publication_script()


def test_a_failed_deploy_step_is_a_named_alert_stage() -> None:
    assert price_refresh_alerts.STAGES[STEP_NAME] == "deploy_dispatch"
    steps = [{"name": "Commit, verify, and push if changed", "number": 27, "conclusion": "success",
              "started_at": iso(NOW), "completed_at": iso(NOW)},
             {"name": STEP_NAME, "number": 31, "conclusion": "failure",
              "started_at": iso(NOW), "completed_at": iso(NOW)}]
    run = {"id": 100, "run_attempt": 1, "head_sha": "a" * 40, "path": ".github/workflows/refresh-prices.yml",
           "head_branch": "main", "repository": {"full_name": price_refresh_alerts.REPOSITORY},
           "event": "schedule", "conclusion": "failure"}
    details = price_refresh_alerts.failed_run(run, [{"steps": steps}])
    assert details is not None
    assert (details["stage"], details["step_number"]) == ("deploy_dispatch", 31)


@dataclass
class StepRun:
    code: int
    output: str
    python: list[list[str]]
    gh: list[list[str]]


def _logged(path: Path) -> list[list[str]]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def _stub(path: Path, body: str) -> None:
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o700)


def _run_step(tmp_path: Path, env: dict[str, str]) -> StepRun:
    bash = shutil.which("bash")
    assert bash, "the workflow step needs bash"
    result = subprocess.run(  # noqa: S603 - actual workflow shell, only python3 and gh are stand-ins
        [bash, "--noprofile", "--norc", "-c", _steps()[-1]["run"]],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        # The step's only external commands are python3 and gh. Nothing else is
        # on PATH, so no real gh or python3 of this machine can answer.
        env={"PATH": str(tmp_path / "bin"), "GITHUB_REPOSITORY": REPO, "PYTHONDONTWRITEBYTECODE": "1", **env},
    )
    return StepRun(result.returncode, result.stdout + result.stderr,
                   _logged(tmp_path / "python3.jsonl"), _logged(tmp_path / "gh.jsonl"))


RECORD = (
    "import json, os, sys\n"
    "def record(name):\n"
    "    with open(os.path.join(os.environ['STUB_DIR'], name + '.jsonl'), 'a') as log:\n"
    "        log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
)


def _step_with_stubs(tmp_path: Path, printed: str, python_exit: int = 0, gh_exit: int = 0) -> StepRun:
    """The step's own shell, with a python3 that prints `printed` and a gh that only records."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _stub(bin_dir / "python3", RECORD + "record('python3')\n"
          "sys.stdout.write(os.environ['STUB_PRINTS'])\nsys.exit(int(os.environ['STUB_PYTHON_EXIT']))\n")
    _stub(bin_dir / "gh", RECORD + "record('gh')\nsys.exit(int(os.environ['STUB_GH_EXIT']))\n")
    return _run_step(tmp_path, {"STUB_DIR": str(tmp_path), "STUB_PRINTS": printed,
                                "STUB_PYTHON_EXIT": str(python_exit), "STUB_GH_EXIT": str(gh_exit)})


ASKED = [["-m", "scripts.deploy.price_snapshot_deploy", "--repo", REPO]]


def test_the_step_dispatches_deploy_on_dispatch(tmp_path: Path) -> None:
    run = _step_with_stubs(tmp_path, "dispatch\n")
    assert run.code == 0, run.output
    assert run.python == ASKED
    assert run.gh == [DISPATCH_ARGV]
    assert "dispatched deploy.yml for a waiting snapshot" in run.output


def test_the_step_fails_when_the_dispatch_fails(tmp_path: Path) -> None:
    run = _step_with_stubs(tmp_path, "dispatch\n", gh_exit=1)
    assert run.code != 0, run.output
    assert run.gh == [DISPATCH_ARGV]
    assert "dispatched deploy.yml" not in run.output


def test_the_step_runs_no_gh_command_on_wait(tmp_path: Path) -> None:
    run = _step_with_stubs(tmp_path, "wait\n")
    assert run.code == 0, run.output
    assert run.python == ASKED
    assert run.gh == []
    assert "no deploy dispatched" in run.output


@pytest.mark.parametrize("printed, python_exit", [
    ("", 0),  # nothing decided
    ("", 1),  # the check's unknown
    ("dispatch\n", 1),  # a failed check decides nothing, whatever it printed
    ("wait\n", 1),
    ("maybe\n", 0),
    ("dispatch\nwait\n", 0),
    ("dispatch now\n", 0),
    ("Dispatch\n", 0),
])
def test_the_step_fails_without_a_gh_command_on_anything_else(tmp_path: Path, printed: str, python_exit: int) -> None:
    run = _step_with_stubs(tmp_path, printed, python_exit)
    assert run.code != 0, run.output
    assert run.python == ASKED
    assert run.gh == []


def _step_with_the_real_check(tmp_path: Path, answers: dict[str, object]) -> StepRun:
    """The step end to end: its shell, the real module under this interpreter, and a gh answering from a table."""

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "answers.json").write_text(json.dumps(answers))
    _stub(bin_dir / "python3", f"import os, sys\nos.execv({sys.executable!r}, [{sys.executable!r}, *sys.argv[1:]])\n")
    _stub(bin_dir / "gh", RECORD + "record('gh')\n"
          "if sys.argv[1:3] == ['workflow', 'run']:\n    sys.exit(0)\n"
          "answers = json.load(open(os.path.join(os.environ['STUB_DIR'], 'answers.json')))\n"
          "asked = ' '.join(sys.argv[1:])\n"
          "if asked not in answers:\n    sys.exit('unexpected gh call: ' + asked)\n"
          "print(json.dumps(answers[asked]))\n")
    return _run_step(tmp_path, {"STUB_DIR": str(tmp_path)})


def test_the_step_end_to_end_dispatches_for_a_snapshot_that_waited_too_long(tmp_path: Path) -> None:
    now = time.time()
    run = _step_with_the_real_check(tmp_path, world([CODE, snapshot(LIMIT + 600, now=now)]))
    assert run.code == 0, run.output
    assert run.gh == [*TO_ACTIVE, DISPATCH_ARGV]
    assert "price_snapshot_deploy: dispatch: " in run.output


def test_the_step_end_to_end_waits_for_a_young_snapshot(tmp_path: Path) -> None:
    now = time.time()
    run = _step_with_the_real_check(tmp_path, world([CODE, snapshot(LIMIT - 600, now=now)]))
    assert run.code == 0, run.output
    assert run.gh == TO_COMPARE
    assert "price_snapshot_deploy: wait: " in run.output


def test_the_step_end_to_end_fails_without_a_dispatch_on_an_unknown(tmp_path: Path) -> None:
    answers = world()
    answers[key(HEAD)] = {"sha": "main"}
    run = _step_with_the_real_check(tmp_path, answers)
    assert run.code != 0, run.output
    assert run.gh == [SUCCESSFUL, view(31), HEAD]
    assert "price_snapshot_deploy: unknown: main's head is not a 40-hex commit id" in run.output
