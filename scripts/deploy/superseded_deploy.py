"""May a superseded control-plane deploy still ship?

deploy.yml runs on pushes to main that touch DEPLOY_PATHS. A run whose commit
is no longer main's head may skip its production steps and leave the deploy
to the newer push's run, but only when that run is ready to take over:

* A newer commit that touches none of DEPLOY_PATHS starts no run of its own,
  so skipping would leave main undeployed. BASE ships: DEPLOY_PATHS covers
  every input of the image and of the deploy itself (tests pin it to the
  Dockerfile and to the trigger), so deploying BASE ships exactly what main
  would.
* Preparation runs outside the production concurrency queue. A newer run
  qualifies only after its exact-commit CI and image build succeed. A run
  with green CI but an unfinished image must not displace this release.
  Skipping before that run can deploy at once starves main: on
  2026-10-21 five control-plane pushes in a row waited for the lease, found
  themselves superseded, skipped, and main went 13 h undeployed. So BASE
  ships now unless a newer push is prepared, green and still on main. The
  production queue retains prepared releases instead of evicting a newer
  release when an older build finishes late. Already delivered successors
  also prevent an older preparation from downgrading production.

    superseded_deploy.py --repo OWNER/NAME --base DEPLOYED_SHA --head MAIN_SHA

Exit 0 to deploy BASE, 1 to skip. A diverged history (BASE no longer on
main), a truncated file list or a failed compare exits 1: BASE may not be a
state main ever had, so the newer run deploys instead. A waiting run counts
as the successor only when it is a push run on main whose commit descends
from BASE and is still on main (equal to or an ancestor of MAIN_SHA); a
manual dispatch, a side-branch run or a rerun of a commit that a force-push
removed cannot advance main and must not make this run skip. A failed,
timed-out or empty lookup of the waiting run, its lineage or its CI exits 0:
the only safe reading of
"unknown" is "not known to be ready", and shipping BASE costs at most one
extra rollout where skipping can starve main.

    superseded_deploy.py --mode ancestry --repo OWNER/NAME --base SHA --head MAIN_SHA

The narrower check deploy.yml runs after the lease is held: exit 0 when BASE
is still on main (the compare is `identical` or `ahead`), 1 otherwise or when
unknown. Main is protected against force-pushes, but a lease wait lasts hours
and this is the last point before production changes.

    superseded_deploy.py --mode cancel-older --repo OWNER/NAME --base SHA

deploy.yml's cancel-superseded job runs this for its own commit. An older push's
deploy that is still preparing would ship a subset of this commit, one rollout
before this one ships all of it, and its CI run of 46 jobs holds the
organization's runners (60 at once) that this commit's run waits for. So it
cancels that deploy, and then its CI run, only when all of these hold:
- the deploy is a push run on main, other than this one, not completed;
- its commit is an ancestor of this one (the compare is `ahead`);
- every job it has is outside the rollout (cancel-superseded, gate-on-ci,
  build-image), so it is neither in the production queue nor rolling out;
- no other deploy run of that commit (a dispatch) is still running: its gate
  reads the same CI run;
- its CI run has not started a test shard. A run already testing is left to
  finish, and its deploy to the supersede rule above.

Cancelling must not keep main undeployed, the starvation of 2026-10-21. Gaps
between deliveries of four hours and more are normal, so the bound is on
cancelling itself: after the last delivered rollout, the first cancellation
opens a window of CANCEL_WINDOW_SECONDS. When it has passed without a
delivery, nothing more is cancelled until a rollout is delivered again. With
no delivery known, nothing is cancelled.

The trade-off: a cancelled older commit no longer ships by itself. If the
superseding push's CI or build then fails, re-run the cancelled CI run and
then its deploy, or dispatch a hotfix. A cancelled deploying commit also gets
no fast path release (fastpath-release.yml builds one only after green CI),
so pin fastpath_release only to a commit whose release exists. A
non-deploying commit's CI run is never touched.

Every unknown answer cancels nothing. Every gh call is capped at the time
left before CANCEL_DEADLINE_SECONDS. The mode always exits 0, since this job
failing would mark a delivered release's run as failed for
reconcile_cloud_releases.py; deploy.yml also runs the job with
continue-on-error.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
import time
from collections.abc import Iterable
from datetime import datetime

# Must equal `on.push.paths` in .github/workflows/deploy.yml (a test pins it).
DEPLOY_PATHS = (
    "src/**",
    "scripts/deploy/**",
    "scripts/entrypoint.sh",
    "frontend/src/**",
    "Dockerfile",
    ".gcloudignore",
    "pyproject.toml",
    "uv.lock",
    ".github/workflows/deploy.yml",
    ".github/workflows/deploy-production.yml",
)

# GitHub's compare API returns at most 300 files.
_COMPARE_FILE_LIMIT = 300

DEPLOY_WORKFLOW = "deploy.yml"
CI_WORKFLOW = "ci.yml"
MAIN_BRANCH = "main"
# One `gh` call; the workflow step has no other bound, and a hung lookup must
# read as "unknown", not as a crash.
GH_TIMEOUT_SECONDS: float = 60
# `gh run list` statuses of a run that has not started: waiting for its
# concurrency group, a runner, or an approval.
_WAITING_RUN_STATUSES = frozenset({"pending", "queued", "waiting", "requested"})
# The rollout jobs whose success proves a release was delivered, with the
# reusable workflow's "rollout / " prefix removed (reconcile_cloud_releases.py
# requires the same set).
_DELIVERY_JOBS = frozenset({"deploy", "rollout-secondaries", "public-surface-companion",
                            "verify-cloud-complete", "finalize-cloud"})
# deploy.yml's jobs outside the rollout: a run whose every job is one of these
# is neither in the production queue nor rolling out. Every push run has its
# own cancel-superseded job.
_PREPARATION_JOBS = frozenset({"cancel-superseded", "gate-on-ci", "build-image"})
# ci.yml's test matrix job is named "test (N)" for shard N.
_TEST_SHARD_PREFIX = "test ("
# After the last delivered rollout, cancel-older may cancel for this long
# from its first cancellation, then stops until a rollout is delivered.
# Replayed over the 62 deploying pushes from 2026-10-28 to 11-06, it allowed
# all 13 cancellations the other rules allowed (a 2 h bound on the time since
# a delivery allowed 5).
CANCEL_WINDOW_SECONDS = 4 * 3600
# cancel-older stops after this long; each gh call is capped at what is left,
# well inside deploy.yml's cancel-superseded timeout-minutes.
CANCEL_DEADLINE_SECONDS: float = 8 * 60
# The deadline's clock (patched in tests; subprocess keeps time.monotonic).
_monotonic = time.monotonic


def touches_deploy_paths(files: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for name in files for pattern in DEPLOY_PATHS)


def changed_files(compare: dict) -> list[str] | None:
    """Every path a compare touched, or None when the answer is incomplete."""

    if compare.get("status") != "ahead":
        return None
    entries = compare.get("files")
    if not isinstance(entries, list) or len(entries) >= _COMPARE_FILE_LIMIT:
        return None
    names: list[str] = []
    for entry in entries:
        names.append(str(entry["filename"]))
        if entry.get("previous_filename"):
            names.append(str(entry["previous_filename"]))
    return names


def may_deploy(compare: dict) -> bool:
    files = changed_files(compare)
    return files is not None and not touches_deploy_paths(files)


def pending_successor(runs: object, *, base: str) -> str | None:
    """The head commit of the newest push run on main still waiting to start, from `gh run list` JSON.

    The listing includes the calling run itself; whatever status GitHub reports
    for it, a run for BASE is never its own successor. A manual dispatch or a run
    on another branch cannot advance main, so it is not a successor either.
    """

    if not isinstance(runs, list):
        return None
    for run in runs:  # newest first
        if not isinstance(run, dict) or run.get("status") not in _WAITING_RUN_STATUSES:
            continue
        if run.get("event") != "push" or run.get("headBranch") != MAIN_BRANCH:
            continue
        sha = run.get("headSha")
        if sha and sha != base:
            return str(sha)
    return None


def ci_green(runs: object) -> bool:
    """Whether the newest CI run in `gh run list` JSON concluded success."""

    if not isinstance(runs, list) or not runs or not isinstance(runs[0], dict):
        return False
    return runs[0].get("status") == "completed" and runs[0].get("conclusion") == "success"


def base_on_main(compare: object) -> bool:
    """Whether the compare's base is on its head's history: the compare is `identical` or `ahead`."""

    return isinstance(compare, dict) and compare.get("status") in ("identical", "ahead")


def _call_timeout(deadline: float | None) -> float | None:
    """GH_TIMEOUT_SECONDS, capped at the time left before DEADLINE; None when none is left."""

    if deadline is None:
        return GH_TIMEOUT_SECONDS
    left = deadline - _monotonic()
    return min(GH_TIMEOUT_SECONDS, left) if left > 0 else None


def _gh_json(args: list[str], deadline: float | None = None) -> object | None:
    """One `gh` call parsed as JSON; None (with the reason on stderr) for any failure, including a timeout."""

    timeout = _call_timeout(deadline)
    if timeout is None:
        print(f"superseded_deploy: out of time before gh {args[0]}", file=sys.stderr)
        return None
    try:
        result = subprocess.run(  # noqa: S603 - fixed gh invocation; arguments are commit SHAs and names
            ["gh", *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"superseded_deploy: gh {args[0]} timed out after {timeout} s", file=sys.stderr)
        return None
    except OSError as exc:
        print(f"superseded_deploy: cannot run gh: {exc}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(f"superseded_deploy: gh {args[0]} failed: {result.stderr.strip()[:300]}", file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"superseded_deploy: gh {args[0]} answered with something other than JSON", file=sys.stderr)
        return None


def successor_ready(repo: str, base: str, head: str) -> tuple[bool, str]:
    """Whether a waiting push run on main can deploy at once, and why.

    Its commit must descend from BASE and still be on main (equal to or an
    ancestor of HEAD, main's head as observed by the caller), and its CI run
    must have concluded success and its image must already be built.
    """

    runs = _gh_json(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--limit", "100",
                     "--json", "databaseId,status,conclusion,headSha,headBranch,event"])
    if not isinstance(runs, list):
        return False, "deploy queue is unknown"
    for run in runs:
        if not isinstance(run, dict) or run.get("event") != "push" or run.get("headBranch") != MAIN_BRANCH:
            continue
        successor = run.get("headSha")
        if not successor or successor == base or not run.get("databaseId"):
            continue
        if run.get("status") not in _WAITING_RUN_STATUSES | {"in_progress", "completed"}:
            continue
        if run.get("status") == "completed" and run.get("conclusion") != "success":
            continue
        lineage = _gh_json(["api", f"repos/{repo}/compare/{base}...{successor}"])
        if not isinstance(lineage, dict) or lineage.get("status") != "ahead":
            continue
        if not base_on_main(_gh_json(["api", f"repos/{repo}/compare/{successor}...{head}"])):
            continue
        ci = _gh_json(["run", "list", "--repo", repo, "--workflow", CI_WORKFLOW, "--commit", successor,
                       "--limit", "1", "--json", "status,conclusion"])
        if not ci_green(ci):
            continue
        details = _gh_json(["run", "view", str(run["databaseId"]), "--repo", repo, "--json", "jobs"])
        if not isinstance(details, dict) or not isinstance(details.get("jobs"), list):
            continue
        jobs = {job.get("name"): job.get("conclusion") for job in details["jobs"] if isinstance(job, dict)}
        if run["status"] == "completed":
            # An older preparation can finish AFTER a newer release ships.
            # Never downgrade it; skipped/partial rollouts do not prove delivery.
            delivered = {str(name).removeprefix("rollout / "): result for name, result in jobs.items()}
            if all(delivered.get(name) == "success" for name in _DELIVERY_JOBS):
                return True, f"newer release {successor[:9]} already completed all production gates"
        elif all(jobs.get(name) == "success" for name in ("gate-on-ci", "build-image")):
            return True, f"newer release {successor[:9]} has green CI and a prepared image queued for rollout"
    return False, "no newer prepared green release can take over; shipping the current eligible release"


def _gh_ok(args: list[str], deadline: float | None = None) -> bool:
    """One `gh` call that answers no JSON (a cancel); whether it succeeded."""

    timeout = _call_timeout(deadline)
    if timeout is None:
        print(f"superseded_deploy: out of time before gh {args[0]} {args[1]}", file=sys.stderr)
        return False
    try:
        result = subprocess.run(  # noqa: S603 - fixed gh invocation; arguments are run ids and names
            ["gh", *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"superseded_deploy: gh {args[0]} {args[1]} did not finish: {exc}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"superseded_deploy: gh {args[0]} {args[1]} failed: {result.stderr.strip()[:300]}", file=sys.stderr)
        return False
    return True


def delivered(jobs: object) -> bool:
    """Whether a deploy run's jobs (`gh run view --json jobs`) show a delivered rollout."""

    if not isinstance(jobs, list):
        return False
    results = {str(job.get("name", "")).removeprefix("rollout / "): job.get("conclusion")
               for job in jobs if isinstance(job, dict)}
    return all(results.get(name) == "success" for name in _DELIVERY_JOBS)


def preparing(jobs: object) -> bool:
    """Whether every job a deploy run has is a preparation job; a run with none has not started."""

    if not isinstance(jobs, list):
        return False
    return all(isinstance(job, dict) and job.get("name") in _PREPARATION_JOBS for job in jobs)


def tests_started(jobs: object) -> bool | None:
    """Whether a CI run has started a test shard; None when its jobs are unknown."""

    if not isinstance(jobs, list):
        return None
    return any(isinstance(job, dict) and str(job.get("name", "")).startswith(_TEST_SHARD_PREFIX)
               and job.get("status") in ("in_progress", "completed") for job in jobs)


def _epoch(value: object) -> float | None:
    """An ISO 8601 time from gh (2026-11-06T17:01:10Z) as epoch seconds; None when it is not one."""

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def last_delivery(repo: str, deadline: float) -> float | None:
    """When the newest delivered deploy run on main last changed, among its 20 newest successful runs.

    A successful run whose rollout was skipped as superseded delivered
    nothing. None when no delivery is found or the answer is unknown.
    """

    runs = _gh_json(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--branch", MAIN_BRANCH,
                     "--status", "success", "--limit", "20", "--json", "databaseId,updatedAt"], deadline)
    if not isinstance(runs, list):
        return None
    for run in runs:  # newest first, by creation
        if not isinstance(run, dict) or not run.get("databaseId"):
            continue
        updated = _epoch(run.get("updatedAt"))
        if updated is None:
            continue
        details = _gh_json(["run", "view", str(run["databaseId"]), "--repo", repo, "--json", "jobs"], deadline)
        if isinstance(details, dict) and delivered(details.get("jobs")):
            return updated
    return None


def cancel_window_open(repo: str, now: float, deadline: float) -> tuple[bool, str]:
    """Whether cancel-older may cancel now, and why (see the module docstring)."""

    delivered_at = last_delivery(repo, deadline)
    if delivered_at is None:
        return False, "no delivered rollout is known"
    runs = _gh_json(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--branch", MAIN_BRANCH,
                     "--status", "cancelled", "--limit", "100", "--json", "databaseId,updatedAt"], deadline)
    if not isinstance(runs, list):
        return False, "the cancelled deploys are unknown"
    since = [stamp for run in runs if isinstance(run, dict)
             for stamp in [_epoch(run.get("updatedAt"))] if stamp is not None and stamp > delivered_at]
    if since and now - min(since) > CANCEL_WINDOW_SECONDS:
        hours = (now - min(since)) / 3600
        return False, (f"the first cancellation since the last delivery was {hours:.1f} h ago, past the "
                       f"{CANCEL_WINDOW_SECONDS // 3600} h window")
    return True, "within the cancel window"


def cancel_older(repo: str, this: str, now: float) -> list[str]:
    """Cancel the preparing deploys of older main pushes that THIS supersedes, then their CI runs.

    Returns one line for each deploy cancelled. See the module docstring for
    what qualifies; any unknown answer leaves the run alone.
    """

    deadline = _monotonic() + CANCEL_DEADLINE_SECONDS
    open_now, why = cancel_window_open(repo, now, deadline)
    if not open_now:
        print(f"superseded_deploy: {why}; cancelling nothing")
        return []
    runs = _gh_json(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--branch", MAIN_BRANCH,
                     "--event", "push", "--limit", "100",
                     "--json", "databaseId,status,headSha,headBranch,event"], deadline)
    if not isinstance(runs, list):
        print("superseded_deploy: the deploy queue is unknown; cancelling nothing")
        return []
    cancelled: list[str] = []
    for run in runs:
        if _monotonic() > deadline:
            print("superseded_deploy: out of time; leaving the remaining runs alone")
            break
        if not isinstance(run, dict) or run.get("event") != "push" or run.get("headBranch") != MAIN_BRANCH:
            continue
        sha, run_id = run.get("headSha"), run.get("databaseId")
        if not sha or sha == this or not run_id:
            continue
        if run.get("status") not in _WAITING_RUN_STATUSES | {"in_progress"}:
            continue
        lineage = _gh_json(["api", f"repos/{repo}/compare/{sha}...{this}", "--jq", "{status: .status}"], deadline)
        if not isinstance(lineage, dict) or lineage.get("status") != "ahead":
            continue
        details = _gh_json(["run", "view", str(run_id), "--repo", repo, "--json", "jobs"], deadline)
        if not isinstance(details, dict) or not preparing(details.get("jobs")):
            continue
        # A dispatch of the same commit would read the CI run cancelled here.
        others = _gh_json(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--commit", str(sha),
                           "--limit", "20", "--json", "databaseId,status"], deadline)
        if not isinstance(others, list) or any(
                not isinstance(other, dict) or (other.get("databaseId") != run_id and other.get("status") != "completed")
                for other in others):
            continue
        ci = _gh_json(["run", "list", "--repo", repo, "--workflow", CI_WORKFLOW, "--commit", str(sha),
                       "--event", "push", "--limit", "1", "--json", "databaseId,status"], deadline)
        if not isinstance(ci, list) or not ci or not isinstance(ci[0], dict) or not ci[0].get("databaseId"):
            continue
        if ci[0].get("status") == "completed":
            continue
        ci_details = _gh_json(["run", "view", str(ci[0]["databaseId"]), "--repo", repo, "--json", "jobs"], deadline)
        if tests_started(ci_details.get("jobs") if isinstance(ci_details, dict) else None) is not False:
            continue
        # The deploy first: cancelled CI under a live deploy would turn that
        # deploy's gate red instead of leaving it cancelled.
        if not _gh_ok(["run", "cancel", str(run_id), "--repo", repo], deadline):
            continue
        ci_cancelled = _gh_ok(["run", "cancel", str(ci[0]["databaseId"]), "--repo", repo], deadline)
        cancelled.append(f"deploy run {run_id} of {str(sha)[:9]}, and its CI run {ci[0]['databaseId']}"
                         + ("" if ci_cancelled else " (its CI run could not be cancelled)"))
    return cancelled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head")
    parser.add_argument("--mode", choices=("supersede", "ancestry", "cancel-older"), default="supersede")
    args = parser.parse_args(argv)
    if args.mode == "cancel-older":
        try:
            cancelled = cancel_older(args.repo, args.base, time.time())
        except Exception as exc:  # noqa: BLE001 - this job must never fail a deploy run
            # An annotation, so a fault here is seen rather than read as "nothing to cancel".
            print(f"::warning::superseded_deploy cancel-older stopped: {type(exc).__name__}: {exc}")
            return 0
        for line in cancelled:
            print(f"superseded_deploy: cancelled {line}, superseded by {args.base[:9]}")
        if not cancelled:
            print(f"superseded_deploy: nothing that {args.base[:9]} supersedes is cancellable")
        return 0
    if not args.head:
        parser.error("--head is required for --mode supersede and --mode ancestry")
    compare = _gh_json(["api", f"repos/{args.repo}/compare/{args.base}...{args.head}"])
    if compare is None:
        return 1
    if args.mode == "ancestry":
        if base_on_main(compare):
            print(f"superseded_deploy: {args.base[:9]} is still on main ({compare.get('status')})")  # type: ignore[union-attr]
            return 0
        status = compare.get("status") if isinstance(compare, dict) else None
        print(f"superseded_deploy: {args.base[:9]} is not on main any more ({status}); not deploying", file=sys.stderr)
        return 1
    files = changed_files(compare if isinstance(compare, dict) else {})
    if files is None:
        status = compare.get("status") if isinstance(compare, dict) else None
        print(f"superseded_deploy: cannot list the changes ({status}); not deploying", file=sys.stderr)
        return 1
    deploy_relevant = sorted(name for name in files if touches_deploy_paths([name]))
    if not deploy_relevant:
        print(f"superseded_deploy: {len(files)} newer file(s), none on a deploy path")
        return 0
    ready, why = successor_ready(args.repo, args.base, args.head)
    changed = ", ".join(deploy_relevant[:10])
    if ready:
        print(f"superseded_deploy: newer commits change deploy paths: {changed}; {why}")
        return 1
    print(f"superseded_deploy: newer commits change deploy paths: {changed}; {why}; deploying {args.base[:9]} now")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
