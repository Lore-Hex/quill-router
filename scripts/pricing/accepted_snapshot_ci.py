"""Wait for an accepted price snapshot's own exact-SHA ci.yml run.

The hourly refresh (.github/workflows/refresh-prices.yml) may publish a
data-only snapshot on the strength of its CI run on an earlier main: the
snapshot commit is carried onto main's newer head and pushed at once instead
of starting a new 20-minute CI run that main's next merge would cancel again
(the publication step's comment lists the five rules). The carried SHA itself
has had no CI run yet, so right after the push the step dispatches one on the
refresh branch, and this step waits for it:

* deploy.yml's gate-on-ci never deploys a SHA without a green push or
  dispatch ci.yml run of that exact SHA, so this run is what lets the
  snapshot roll out at all;
* a red run means main holds a combination of code and data that fails its
  tests. Nothing deploys it, but every later commit inherits it, so this step
  fails the refresh run loudly (the refresh-failed alert fires) and says how
  to revert.

One rerun of only the failed jobs absorbs a single flaky shard, as the
snapshot's own CI poll does. The run is found by the refresh branch and the
exact SHA, then polled by id.

    python3 -m scripts.pricing.accepted_snapshot_ci --repo OWNER/NAME --sha SHA --branch BRANCH

Exit 0 when the run concluded success, 1 otherwise. Stdlib only.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Sequence

CI_WORKFLOW = "ci.yml"
POLL_SECONDS: float = 15
# A data-only run takes 13-28 minutes; a rerun of one shard about 12 more.
TIMEOUT_SECONDS: float = 90 * 60
# The dispatch was made just before this step: a run not listed by then never will be.
FIND_SECONDS: float = 10 * 60
RUN_FIELDS = "databaseId,headSha,status,conclusion,attempt"

# The pause and the clock (patched in tests).
_sleep = time.sleep
_now = time.monotonic


def _gh(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed gh invocation; arguments are a SHA, a run id and names
        ["gh", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _gh_json(args: Sequence[str]) -> object | None:
    """One gh read parsed as JSON; None (reason on stderr) for any failure."""

    try:
        result = _gh(args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"accepted_snapshot_ci: gh {args[0]} failed: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"accepted_snapshot_ci: gh {args[0]} failed: {result.stderr.strip()[:300]}", file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError:
        print(f"accepted_snapshot_ci: gh {args[0]} answered with something other than JSON", file=sys.stderr)
        return None


def _gh_ok(args: Sequence[str]) -> bool:
    try:
        result = _gh(args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"accepted_snapshot_ci: gh {' '.join(args[:2])} failed: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"accepted_snapshot_ci: gh {' '.join(args[:2])} failed: {result.stderr.strip()[:300]}",
              file=sys.stderr)
    return result.returncode == 0


def find_run(repo: str, branch: str, sha: str) -> dict | None:
    """The dispatched ci.yml run of exactly SHA on BRANCH, newest first; None when not listed (yet)."""

    runs = _gh_json(["run", "list", "--repo", repo, "--workflow", CI_WORKFLOW, "--branch", branch,
                     "--event", "workflow_dispatch", "--json", RUN_FIELDS])
    if not isinstance(runs, list):
        return None
    return next((run for run in runs if isinstance(run, dict) and run.get("headSha") == sha), None)


def view_run(repo: str, run_id: int) -> dict | None:
    run = _gh_json(["run", "view", str(run_id), "--repo", repo, "--json", RUN_FIELDS])
    return run if isinstance(run, dict) else None


def revert_hint(sha: str) -> str:
    return (f"main holds accepted snapshot {sha}, whose own CI run did not pass. deploy.yml deploys "
            f"neither it nor any later commit until main is green again. If the snapshot's data is "
            f"the cause, revert it: git revert --no-edit {sha}")


def wait(repo: str, sha: str, branch: str) -> int:
    start = _now()
    run_id: int | None = None
    rerun_attempt: int | None = None
    while True:
        elapsed = _now() - start
        if run_id is None:
            if elapsed >= FIND_SECONDS:
                print(f"::error title=Accepted snapshot CI::no ci.yml run of {sha} on {branch} appeared; "
                      + revert_hint(sha))
                return 1
            run = find_run(repo, branch, sha)
        else:
            if elapsed >= TIMEOUT_SECONDS:
                print(f"::error title=Accepted snapshot CI::ci run {run_id} of {sha} did not finish in "
                      f"{TIMEOUT_SECONDS / 60:g} minutes; cancelling it. " + revert_hint(sha))
                _gh_ok(["run", "cancel", str(run_id), "--repo", repo])
                return 1
            run = view_run(repo, run_id)
            if run is not None and run.get("headSha") != sha:
                print(f"::error title=Accepted snapshot CI::ci run {run_id} no longer reports {sha}")
                return 1
        if run is not None:
            run_id = int(run["databaseId"])
            attempt = int(run.get("attempt") or 1)
            status, conclusion = run.get("status"), run.get("conclusion")
            print(f"  ci run {run_id} attempt {attempt}: status={status} conclusion={conclusion or '<pending>'}")
            if status == "completed" and (rerun_attempt is None or attempt >= rerun_attempt):
                if conclusion == "success":
                    print(f"accepted snapshot {sha} passed its own CI run {run_id}")
                    return 0
                if conclusion == "failure" and rerun_attempt is None:
                    print(f"::warning title=Accepted snapshot CI rerun::ci run {run_id} attempt {attempt} "
                          "failed; rerunning its failed jobs once")
                    if not _gh_ok(["run", "rerun", str(run_id), "--failed", "--repo", repo]):
                        print("::error title=Accepted snapshot CI::could not rerun the failed jobs; "
                              + revert_hint(sha))
                        return 1
                    rerun_attempt = attempt + 1
                else:
                    print(f"::error title=Accepted snapshot CI::ci run {run_id} concluded '{conclusion}'; "
                          + revert_hint(sha))
                    return 1
        _sleep(POLL_SECONDS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo", required=True)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--branch", required=True)
    args = parser.parse_args(argv)
    return wait(args.repo, args.sha, args.branch)


if __name__ == "__main__":
    raise SystemExit(main())
