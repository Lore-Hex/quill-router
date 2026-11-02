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
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import subprocess
import sys
from collections.abc import Iterable

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


def _gh_json(args: list[str]) -> object | None:
    """One `gh` call parsed as JSON; None (with the reason on stderr) for any failure, including a timeout."""

    try:
        result = subprocess.run(  # noqa: S603 - fixed gh invocation; arguments are commit SHAs and names
            ["gh", *args],  # noqa: S607
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        print(f"superseded_deploy: gh {args[0]} timed out after {GH_TIMEOUT_SECONDS} s", file=sys.stderr)
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
            required = {"deploy", "rollout-secondaries", "public-surface-companion",
                        "verify-cloud-complete", "finalize-cloud"}
            delivered = {str(name).removeprefix("rollout / "): result for name, result in jobs.items()}
            if all(delivered.get(name) == "success" for name in required):
                return True, f"newer release {successor[:9]} already completed all production gates"
        elif all(jobs.get(name) == "success" for name in ("gate-on-ci", "build-image")):
            return True, f"newer release {successor[:9]} has green CI and a prepared image queued for rollout"
    return False, "no newer prepared green release can take over; shipping the current eligible release"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--mode", choices=("supersede", "ancestry"), default="supersede")
    args = parser.parse_args(argv)
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
