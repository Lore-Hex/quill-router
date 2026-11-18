"""Open, update or close the provider-check sync issue.

The public Lore-Hex/trustedrouter-provider-check package vendors this
repository's provider-check contract (src/trusted_router/data/
provider_check_contract.json). .github/workflows/provider-check-sync.yml runs
tests/test_provider_check_contract_parity.py::
test_optional_public_provider_check_snapshot against a checkout of it every
day, off the deploy gate: ci.yml never depends on the public repository's
state, so a contract change here can merge and deploy before the public copy
follows. A failing (or unrunnable) check opens the issue titled ISSUE_TITLE,
or comments on the open one, and the next pass closes it, the way
scripts/ci/testmap.py's heavy-issue handles the heavy lane's scope issue.

Only the bot's own issue counts: exact title, author app/github-actions with
is_bot true, so an issue anyone else opens with the same title is never
commented on or closed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

ISSUE_TITLE = "The public provider-check contract snapshot is out of sync"
ISSUE_AUTHOR = "app/github-actions"
# GitHub rejects issue bodies and comments past 65,536 characters; keep the
# end of the report, where pytest prints the failure.
MAX_REPORT = 60_000
NO_REPORT = "The provider-check sync check failed before writing a report; see the run."
REMEDY = ("Re-run scripts/export_provider_check_contract.py, review the diff, and open a sync PR to "
          "Lore-Hex/trustedrouter-provider-check so its src/tr_provider_check/data/contract_snapshot.json "
          "matches src/trusted_router/data/provider_check_contract.json.")


def issue(outcome: str, report: str, *, repo: str, run_url: str,
          gh: Callable[[list[str], str | None], str]) -> str:
    """What happened to the sync issue: on a failure, comment the report on
    the open issue or open one; on a pass, close the open one. `gh` runs the
    gh command line (argv, stdin)."""
    listed = json.loads(gh(["issue", "list", "--repo", repo, "--state", "open", "--search",
                            f'in:title "{ISSUE_TITLE}" author:{ISSUE_AUTHOR}',
                            "--json", "number,title,author", "--limit", "100"], None) or "[]")
    # The bot's own, by exact title, oldest first (issue numbers only grow).
    numbers = sorted(found["number"] for found in listed if found.get("title") == ISSUE_TITLE
                     and (found.get("author") or {}).get("login") == ISSUE_AUTHOR
                     and (found.get("author") or {}).get("is_bot") is True)
    if outcome == "failure":
        text = report.strip() or NO_REPORT
        if len(text) > MAX_REPORT:
            text = "(report truncated to its end)\n" + text[-MAX_REPORT:]
        body = f"{REMEDY}\n\n```\n{text}\n```\n\nRun: {run_url}\n"
        if numbers:
            gh(["issue", "comment", str(numbers[0]), "--repo", repo, "--body-file", "-"], body)
            return f"commented on #{numbers[0]}"
        gh(["issue", "create", "--repo", repo, "--title", ISSUE_TITLE, "--body-file", "-"], body)
        return "opened an issue"
    for number in numbers:
        gh(["issue", "close", str(number), "--repo", repo, "--comment",
            f"The public provider-check contract snapshot matches again: {run_url}"], None)
    return f"closed {', '.join(f'#{n}' for n in numbers)}" if numbers else "no open issue"


def _gh(argv: list[str], stdin: str | None) -> str:
    return subprocess.run(["gh", *argv], input=stdin, capture_output=True, text=True, check=True).stdout  # noqa: S603,S607


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--outcome", choices=("failure", "success"), required=True)
    parser.add_argument("--report", help="the check's output (on a failure)")
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--run-id", default=os.environ.get("GITHUB_RUN_ID", ""))
    args = parser.parse_args(argv)
    try:
        report = Path(args.report).read_text(encoding="utf-8", errors="replace") if args.report else ""
    except OSError:
        report = ""
    run_url = f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{args.repo}/actions/runs/{args.run_id}"
    print(issue(args.outcome, report, repo=args.repo, run_url=run_url, gh=_gh))
    return 0


if __name__ == "__main__":
    sys.exit(main())
