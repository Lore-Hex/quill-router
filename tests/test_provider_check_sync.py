"""The daily public provider-check snapshot check, off the deploy gate.

.github/workflows/provider-check-sync.yml runs
test_optional_public_provider_check_snapshot against a checkout of
Lore-Hex/trustedrouter-provider-check; scripts/ci/provider_check_sync.py
opens, updates or closes the bot's sync issue.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from scripts.ci import provider_check_sync as sync

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/provider-check-sync.yml"
BOT = {"is_bot": True, "login": sync.ISSUE_AUTHOR}
OUTSIDER = {"is_bot": False, "login": "someone"}
RUN = "https://github.com/o/r/actions/runs/7"


class FakeGh:
    LIST = ["issue", "list", "--repo", "o/r", "--state", "open", "--search",
            f'in:title "{sync.ISSUE_TITLE}" author:{sync.ISSUE_AUTHOR}', "--json", "number,title,author",
            "--limit", "100"]

    def __init__(self, issues: list[dict]) -> None:
        self.issues = issues
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, argv: list[str], stdin: str | None) -> str:
        self.calls.append((argv, stdin))
        if argv[:2] == ["issue", "list"]:
            assert argv == self.LIST and stdin is None
            return json.dumps(self.issues)
        return ""


def issue(number: int, author: dict = BOT, title: str = sync.ISSUE_TITLE) -> dict:
    return {"number": number, "title": title, "author": author}


def test_a_failing_check_opens_the_issue_or_comments_on_the_oldest_open_one() -> None:
    gh = FakeGh([issue(3, title="Something else")])
    assert sync.issue("failure", "AssertionError: drift\n", repo="o/r", run_url=RUN, gh=gh) == "opened an issue"
    argv, body = gh.calls[-1]
    assert argv == ["issue", "create", "--repo", "o/r", "--title", sync.ISSUE_TITLE, "--body-file", "-"]
    assert body == f"{sync.REMEDY}\n\n```\nAssertionError: drift\n```\n\nRun: {RUN}\n"
    gh = FakeGh([issue(12), issue(9), issue(3, title="Something else")])
    assert sync.issue("failure", "AssertionError: drift", repo="o/r", run_url=RUN, gh=gh) == "commented on #9"
    assert gh.calls[-1][0] == ["issue", "comment", "9", "--repo", "o/r", "--body-file", "-"]
    assert len(gh.calls) == 2


def test_an_issue_someone_else_opened_with_the_title_is_never_used() -> None:
    spoofs = [issue(2, OUTSIDER), issue(4, {"is_bot": False, "login": sync.ISSUE_AUTHOR}), issue(5, {})]
    gh = FakeGh(spoofs)
    assert sync.issue("failure", "x", repo="o/r", run_url=RUN, gh=gh) == "opened an issue"
    gh = FakeGh([*spoofs, issue(7)])
    assert sync.issue("failure", "x", repo="o/r", run_url=RUN, gh=gh) == "commented on #7"
    gh = FakeGh(spoofs)
    assert sync.issue("success", "", repo="o/r", run_url=RUN, gh=gh) == "no open issue"
    assert [argv[:2] for argv, _ in gh.calls] == [["issue", "list"]]


def test_a_passing_check_closes_the_bots_open_issues_and_nothing_else() -> None:
    gh = FakeGh([issue(9), issue(3, title="Something else"), issue(2, OUTSIDER)])
    assert sync.issue("success", "", repo="o/r", run_url=RUN, gh=gh) == "closed #9"
    assert gh.calls[-1] == (["issue", "close", "9", "--repo", "o/r", "--comment",
                             f"The public provider-check contract snapshot matches again: {RUN}"], None)


def test_a_check_that_never_ran_or_wrote_too_much_still_reports(tmp_path: Path, monkeypatch, capsys) -> None:
    # No report (the checkout or the sync failed first): the issue says so.
    gh = FakeGh([])
    sync.issue("failure", "", repo="o/r", run_url=RUN, gh=gh)
    assert sync.NO_REPORT in gh.calls[-1][1]
    # A long diff keeps its end, inside GitHub's comment limit.
    gh = FakeGh([])
    sync.issue("failure", "x" * (2 * sync.MAX_REPORT) + "THE END", repo="o/r", run_url=RUN, gh=gh)
    body = gh.calls[-1][1]
    assert "THE END" in body and "(report truncated to its end)" in body and len(body) < 65_536
    # The command line: a missing report file is a report-less failure.
    calls = []
    monkeypatch.setattr(sync, "_gh", lambda argv, stdin: calls.append((argv, stdin)) or "[]")
    monkeypatch.setenv("GITHUB_SERVER_URL", "https://github.com")
    assert sync.main(["--outcome", "failure", "--report", str(tmp_path / "missing.txt"),
                      "--repo", "o/r", "--run-id", "7"]) == 0
    assert calls[-1][0][:2] == ["issue", "create"] and sync.NO_REPORT in calls[-1][1]
    assert RUN in calls[-1][1]
    assert capsys.readouterr().out.strip() == "opened an issue"


def test_the_sync_check_runs_off_the_gate_with_the_token_on_the_issue_steps_only() -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    # Scheduled and dispatchable; never a pull request or push trigger.
    triggers = workflow.get("on", workflow.get(True))
    assert set(triggers) == {"schedule", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    (name, job), = workflow["jobs"].items()
    assert job["permissions"] == {"contents": "read", "issues": "write"}
    assert "env" not in job and "continue-on-error" not in json.dumps(job)
    checkouts = [step for step in job["steps"] if "actions/checkout" in step.get("uses", "")]
    assert len(checkouts) == 2
    assert all(step["with"]["persist-credentials"] is False for step in checkouts)
    public = checkouts[1]["with"]
    assert public["repository"] == "Lore-Hex/trustedrouter-provider-check"
    assert public["sparse-checkout"] == "src/tr_provider_check/data"
    runs = {step["name"]: step for step in job["steps"] if "run" in step}
    token = {step_name for step_name, step in runs.items() if "GH_TOKEN" in json.dumps(step)}
    assert token == {"Open or update the provider-check sync issue", "Close the provider-check sync issue"}
    check = runs["The public snapshot matches this repository's contract"]
    assert check["id"] == "check" and "if" not in check
    assert check["env"] == {"PROVIDER_CHECK_REPO_PATH": "${{ github.workspace }}/" + public["path"]}
    assert "test_optional_public_provider_check_snapshot" in check["run"]
    assert check["run"].startswith("set -o pipefail\n")
    opens = runs["Open or update the provider-check sync issue"]
    closes = runs["Close the provider-check sync issue"]
    assert opens["if"] == "failure() && steps.check.outcome != 'success'"
    assert opens["run"] == "python3 scripts/ci/provider_check_sync.py --outcome failure --report provider-check-sync.txt"
    assert closes["if"] == "success()"
    assert closes["run"] == "python3 scripts/ci/provider_check_sync.py --outcome success"
    # The deploy gate never reads the public repository.
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "trustedrouter-provider-check" not in ci and "PROVIDER_CHECK_REPO_PATH" not in ci


def test_only_a_pass_passes_the_check_step_never_a_skip() -> None:
    check = next(step for step in yaml.safe_load(WORKFLOW.read_text())["jobs"]["sync"]["steps"]
                 if step.get("id") == "check")
    gate = re.search(r'grep -Eq "([^"]+)" provider-check-sync.txt$', check["run"].strip())
    assert gate is not None
    pattern = re.compile(gate[1], re.MULTILINE)
    assert pattern.search("...\n1 passed, 6 warnings in 0.26s\n")
    for summary in ("1 skipped, 6 warnings in 0.10s", "1 failed, 6 warnings in 0.26s",
                    "no tests ran in 0.01s", "ERROR: not found", "11 passed in 1.00s"):
        assert not pattern.search(f"...\n{summary}\n"), summary
