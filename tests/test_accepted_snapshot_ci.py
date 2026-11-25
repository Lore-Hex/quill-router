"""The refresh's wait for an accepted snapshot's own exact-SHA CI run."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence

import pytest

from scripts.pricing import accepted_snapshot_ci as check

REPO = "fixture/quill-router"
SHA = "a" * 40
BRANCH = "automation/price-refresh-42"


def run(status: str, conclusion: str | None = None, attempt: int = 1, sha: str = SHA, run_id: int = 7) -> dict:
    return {"databaseId": run_id, "headSha": sha, "status": status, "conclusion": conclusion, "attempt": attempt}


class FakeGh:
    """Answers each list/view with the next scripted run (repeating the last); records every call."""

    def __init__(self, answers: list[object], *, rerun_ok: bool = True) -> None:
        self.answers = answers
        self.calls: list[list[str]] = []
        self.rerun_ok = rerun_ok
        self.clock = 0.0

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        import json

        args = list(args)
        self.calls.append(args)
        if args[:2] in (["run", "rerun"], ["run", "cancel"]):
            ok = self.rerun_ok or args[1] == "cancel"
            return subprocess.CompletedProcess(args, 0 if ok else 1, "", "" if ok else "boom")
        assert args[:2] in (["run", "list"], ["run", "view"]), args
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if args[1] == "list":
            assert args[args.index("--branch") + 1] == BRANCH
            assert args[args.index("--event") + 1] == "workflow_dispatch"
            assert args[args.index("--workflow") + 1] == "ci.yml"
            answer = [] if answer is None else [run("completed", "success", sha="b" * 40, run_id=3), answer]
        elif answer is None:
            return subprocess.CompletedProcess(args, 1, "", "api error")
        return subprocess.CompletedProcess(args, 0, json.dumps(answer), "")

    def sleep(self, seconds: float) -> None:
        self.clock += seconds


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch):
    def install(answers: list[object], **kwargs: bool) -> FakeGh:
        fake = FakeGh(answers, **kwargs)
        monkeypatch.setattr(check, "_gh", fake)
        monkeypatch.setattr(check, "_sleep", fake.sleep)
        monkeypatch.setattr(check, "_now", lambda: fake.clock)
        return fake
    return install


def verbs(fake: FakeGh) -> list[str]:
    return [" ".join(call[:2]) for call in fake.calls]


def test_waits_for_the_exact_sha_run_to_succeed(gh, capsys: pytest.CaptureFixture[str]) -> None:
    fake = gh([None, run("queued"), run("in_progress"), run("completed", "success")])
    assert check.main(["--repo", REPO, "--sha", SHA, "--branch", BRANCH]) == 0
    # Found by listing, then polled by id; the other SHA's green run never counts.
    assert verbs(fake) == ["run list", "run list", "run view", "run view"]
    assert fake.calls[-1][2] == "7"
    assert "passed its own CI run 7" in capsys.readouterr().out


@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out"])
def test_a_red_run_fails_loudly_with_the_revert(gh, capsys: pytest.CaptureFixture[str], conclusion: str) -> None:
    answers: list[object] = [run("completed", conclusion)]
    if conclusion == "failure":  # one rerun of the failed jobs, which fails too
        answers = [run("completed", "failure"), run("completed", "failure"), run("completed", "failure", attempt=2)]
    fake = gh(answers)
    assert check.wait(REPO, SHA, BRANCH) == 1
    out = capsys.readouterr().out
    assert "::error title=Accepted snapshot CI::" in out
    assert f"git revert --no-edit {SHA}" in out
    assert verbs(fake).count("run rerun") == (1 if conclusion == "failure" else 0)


def test_a_single_flaky_failure_is_rerun_once(gh) -> None:
    fake = gh([run("completed", "failure"), run("completed", "failure"),
               run("in_progress", attempt=2), run("completed", "success", attempt=2)])
    assert check.wait(REPO, SHA, BRANCH) == 0
    reruns = [call for call in fake.calls if call[:2] == ["run", "rerun"]]
    assert reruns == [["run", "rerun", "7", "--failed", "--repo", REPO]]


def test_the_failed_attempt_is_not_read_as_the_rerun(gh) -> None:
    # Just after the rerun request, the view still shows attempt 1's failure.
    fake = gh([run("completed", "failure"), run("completed", "failure"), run("completed", "failure"),
               run("completed", "success", attempt=2)])
    assert check.wait(REPO, SHA, BRANCH) == 0
    assert verbs(fake).count("run rerun") == 1


def test_a_failed_rerun_request_fails(gh) -> None:
    fake = gh([run("completed", "failure")], rerun_ok=False)
    assert check.wait(REPO, SHA, BRANCH) == 1
    assert verbs(fake).count("run rerun") == 1


def test_a_run_that_never_appears_fails(gh, capsys: pytest.CaptureFixture[str]) -> None:
    fake = gh([None])
    assert check.wait(REPO, SHA, BRANCH) == 1
    assert fake.clock >= check.FIND_SECONDS
    assert "no ci.yml run of" in capsys.readouterr().out


def test_a_run_that_never_finishes_is_cancelled(gh) -> None:
    fake = gh([run("in_progress")])
    assert check.wait(REPO, SHA, BRANCH) == 1
    assert fake.calls[-1] == ["run", "cancel", "7", "--repo", REPO]
    assert fake.clock >= check.TIMEOUT_SECONDS


def test_a_view_of_another_sha_fails(gh) -> None:
    gh([run("in_progress"), run("in_progress", sha="c" * 40)])
    assert check.wait(REPO, SHA, BRANCH) == 1


def test_a_failed_view_is_asked_again(gh) -> None:
    fake = gh([run("in_progress"), None, run("completed", "success")])
    assert check.wait(REPO, SHA, BRANCH) == 0
    assert verbs(fake) == ["run list", "run view", "run view"]
