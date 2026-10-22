"""May a superseded control-plane deploy still ship?

deploy.yml runs on pushes to main that touch DEPLOY_PATHS. A run whose commit
is no longer main's head may skip its production steps and leave the deploy
to the newer push's run, but only when that run is ready to take over:

* A newer commit that touches none of DEPLOY_PATHS starts no run of its own,
  so skipping would leave main undeployed. BASE ships: DEPLOY_PATHS covers
  every input of the image and of the deploy itself (tests pin it to the
  Dockerfile and to the trigger), so deploying BASE ships exactly what main
  would.
* The newer run queues behind this one (one pending run per concurrency
  group), then waits for its own CI, which a saturated runner queue holds for
  hours, then for the production lease, which an enclave rollout holds for
  about three. Skipping before that run can deploy at once starves main: on
  2026-10-21 five control-plane pushes in a row waited for the lease, found
  themselves superseded, skipped, and main went 13 h undeployed. So BASE
  ships now unless a deploy.yml run is waiting for a newer commit AND that
  commit's CI run has already concluded success; then the waiting run deploys
  within minutes of this one exiting, and the obsolete image need not roll
  through production first.

    superseded_deploy.py --repo OWNER/NAME --base DEPLOYED_SHA --head MAIN_SHA

Exit 0 to deploy BASE, 1 to skip. A diverged history (BASE no longer on
main), a truncated file list or a failed compare exits 1: BASE may not be a
state main ever had, so the newer run deploys instead. A failed or empty
lookup of the waiting run or of its CI exits 0: the only safe reading of
"unknown" is "not known to be ready", and shipping BASE costs at most one
extra rollout where skipping can starve main.
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
)

# GitHub's compare API returns at most 300 files.
_COMPARE_FILE_LIMIT = 300

DEPLOY_WORKFLOW = "deploy.yml"
CI_WORKFLOW = "ci.yml"
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
    """The head commit of the newest deploy run still waiting to start, from `gh run list` JSON.

    The listing includes the calling run itself; whatever status GitHub reports
    for it, a run for BASE is never its own successor.
    """

    if not isinstance(runs, list):
        return None
    for run in runs:  # newest first
        if not isinstance(run, dict) or run.get("status") not in _WAITING_RUN_STATUSES:
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


def _gh_json(args: list[str]) -> object | None:
    result = subprocess.run(  # noqa: S603 - fixed gh invocation; arguments are commit SHAs and names
        ["gh", *args],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        print(f"superseded_deploy: gh {args[0]} failed: {result.stderr.strip()[:300]}", file=sys.stderr)
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"superseded_deploy: gh {args[0]} answered with something other than JSON", file=sys.stderr)
        return None


def successor_ready(repo: str, base: str) -> tuple[bool, str]:
    """Whether a deploy run for a commit other than BASE is waiting and can deploy at once, and why."""

    runs = _gh_json(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--limit", "10",
                     "--json", "databaseId,status,headSha"])
    successor = pending_successor(runs, base=base)
    if successor is None:
        return False, "no newer deploy run is waiting to take over"
    ci = _gh_json(["run", "list", "--repo", repo, "--workflow", CI_WORKFLOW, "--commit", successor,
                   "--limit", "1", "--json", "status,conclusion"])
    if ci_green(ci):
        return True, f"the waiting run for {successor[:9]} has green CI and deploys at once"
    state = "unknown"
    if isinstance(ci, list) and ci and isinstance(ci[0], dict):
        state = f"{ci[0].get('status')}/{ci[0].get('conclusion') or '-'}"
    return False, f"the waiting run for {successor[:9]} cannot deploy yet (CI {state})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    args = parser.parse_args(argv)
    compare = _gh_json(["api", f"repos/{args.repo}/compare/{args.base}...{args.head}"])
    if compare is None:
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
    ready, why = successor_ready(args.repo, args.base)
    changed = ", ".join(deploy_relevant[:10])
    if ready:
        print(f"superseded_deploy: newer commits change deploy paths: {changed}; {why}")
        return 1
    print(f"superseded_deploy: newer commits change deploy paths: {changed}; {why}; deploying {args.base[:9]} now")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
