"""Does a published price snapshot need a rollout of its own?

The hourly price refresh (.github/workflows/refresh-prices.yml) pushes a
snapshot commit to main. The snapshot lives under src/, so every later
rollout of main ships it. A rollout holds the production lease for over an
hour, and a rollout for each snapshot spent several of those a day on nothing
but prices while code waited in the queue. So a snapshot rides the next
control-plane rollout, and gets one of its own only when no rollout has
carried it for SNAPSHOT_MAX_WAIT_SECONDS. The trade is deliberate: prices
reach production later, code reaches it sooner.

A snapshot must never wait without a bound. A GITHUB_TOKEN push starts no
push-triggered workflow, so nothing deploys a snapshot unless a later push or
this check does: on 2026-05-13 the deployed catalog was stuck for hours
because deploy.yml never fired for the bot's commits. The refresh job runs
this check as its last step on every run, whether or not that run published
anything, because the snapshot that waits was published by an earlier run.

    python3 -m scripts.deploy.price_snapshot_deploy --repo OWNER/NAME

It prints one word to stdout, `dispatch` or `wait`, and exits 0. Its reasons
go to stderr. On `dispatch` the caller dispatches deploy.yml on main with
track_main=true. The rule, in order:

1. The delivered release is the commit of the newest deploy.yml run on main
   that delivered a rollout, among its 20 newest successful runs. A
   successful run whose rollout was skipped as superseded delivered nothing.
2. Main's head is the delivered release: nothing is undelivered, so wait.
3. The waiting snapshots are the commits main has beyond the delivered
   release whose subject starts with SNAPSHOT_SUBJECT_PREFIX. With none,
   wait: a code commit that deploys has its own push run.
4. The oldest waiting snapshot is younger than the limit: wait. A rollout
   may still carry it.
5. A deploy.yml run exists for main's head: wait. In flight, that run is the
   carrier. Completed, a deploy of exactly this head was already tried (it
   failed, a person cancelled it, or a deliberate redeploy of an older
   release followed), and a person decides what happens next. This bounds
   the automatic dispatches to one per main head.
6. A deploy.yml run on main has not completed and its commit contains the
   oldest waiting snapshot: wait, that run carries it. A run of a commit
   from before the snapshot does not.
7. Otherwise dispatch.

Every answer the rule needs and cannot get is "unknown": a gh call that
fails GH_ATTEMPTS times in a row, a commit id that is not 40 hex digits, a
main that is not ahead of the delivered release, a commit list the API
truncated, a snapshot without a readable committer date, a run list of an
unexpected shape. Then the reason goes to stderr, nothing goes to stdout and
the exit code is 1. The workflow step fails where a person sees it, and the
next refresh run asks again. The check never guesses: a guessed `dispatch`
spends a rollout, and a guessed `wait` hides a snapshot that is stuck.
"""

from __future__ import annotations

import argparse
import re
import sys
import time

from scripts.deploy.superseded_deploy import (
    DEPLOY_WORKFLOW,
    MAIN_BRANCH,
    _epoch,
    _gh_json,
    delivered,
)

# The subject refresh-prices.yml gives every snapshot commit, up to its
# timestamp (a test pins the workflow's commit message to this).
SNAPSHOT_SUBJECT_PREFIX = "[bot] hourly price refresh"
# How long a snapshot may wait for a rollout to carry it, from its commit's
# committer date. The commit is made before its exact-SHA CI run (most of an
# hour), and the check runs once per refresh run (hourly when GitHub starts
# every scheduled run), so this is roughly three hours on main.
SNAPSHOT_MAX_WAIT_SECONDS = 4 * 3600
# The check runs every hour, and one API blip must not turn the refresh run
# red: each gh call is asked this many times, this far apart.
GH_ATTEMPTS = 3
GH_RETRY_SECONDS: float = 5
# The retry's pause and the clock (patched in tests).
_sleep = time.sleep
_now = time.time

DISPATCH = "dispatch"
WAIT = "wait"
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")


class Unknown(Exception):
    """An answer the rule needs is unknown; the message says which."""


def _gh(args: list[str]) -> object:
    """One `gh` answer parsed as JSON, asked up to GH_ATTEMPTS times; Unknown when none answers."""

    for attempt in range(1, GH_ATTEMPTS + 1):
        answer = _gh_json(args)
        if answer is not None:
            return answer
        if attempt < GH_ATTEMPTS:
            _sleep(GH_RETRY_SECONDS)
    raise Unknown(f"gh {' '.join(args[:2])} gave no answer in {GH_ATTEMPTS} attempts")


def commit_sha(value: object, what: str) -> str:
    """VALUE when it is a full commit id; Unknown otherwise, naming WHAT it was read as."""

    if not isinstance(value, str) or not _COMMIT_SHA.fullmatch(value):
        raise Unknown(f"{what} is not a 40-hex commit id")
    return value


def undelivered_commits(compare: object) -> list[dict]:
    """The commits main has beyond the delivered release (the compare API's answer); none when they are identical."""

    if not isinstance(compare, dict):
        raise Unknown("the compare of the delivered release with main is not an object")
    status = compare.get("status")
    if status == "identical":
        return []
    if status != "ahead":
        raise Unknown(f"main is not ahead of the delivered release (compare status: {status})")
    commits = compare.get("commits")
    # The API lists at most 250 commits; a snapshot could be among the rest.
    if not isinstance(commits, list) or not commits or compare.get("total_commits") != len(commits):
        listed = len(commits) if isinstance(commits, list) else "no"
        raise Unknown(f"the compare lists {listed} of {compare.get('total_commits')} undelivered commits")
    if not all(isinstance(entry, dict) for entry in commits):
        raise Unknown("an undelivered commit is not an object")
    return commits


def oldest_waiting_snapshot(commits: list[dict]) -> tuple[str, float] | None:
    """The snapshot commit with the earliest committer date, as (id, epoch seconds); None when no commit is one."""

    waiting: list[tuple[float, str]] = []
    for entry in commits:
        commit = entry.get("commit")
        message = commit.get("message") if isinstance(commit, dict) else None
        if not isinstance(commit, dict) or not isinstance(message, str):
            raise Unknown("an undelivered commit has no message")
        if not message.split("\n", 1)[0].startswith(SNAPSHOT_SUBJECT_PREFIX):
            continue
        sha = commit_sha(entry.get("sha"), "a waiting snapshot's id")
        committer = commit.get("committer")
        committed = _epoch(committer.get("date")) if isinstance(committer, dict) else None
        if committed is None:
            raise Unknown(f"snapshot {sha[:9]} has no readable committer date")
        waiting.append((committed, sha))
    if not waiting:
        return None
    committed, sha = min(waiting)
    return sha, committed


def waited_too_long(committed: float, now: float) -> bool:
    return now - committed >= SNAPSHOT_MAX_WAIT_SECONDS


def active_main_heads(runs: object) -> list[str]:
    """The distinct commits of the deploy runs on main that have not completed, from `gh run list` JSON."""

    if not isinstance(runs, list):
        raise Unknown("the deploy runs on main are not a list")
    heads: list[str] = []
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("status"), str) or not run["status"]:
            raise Unknown("a deploy run on main has no status")
        if run["status"] == "completed" or run.get("headBranch") != MAIN_BRANCH:
            continue
        head = commit_sha(run.get("headSha"), f"the commit of active deploy run {run.get('databaseId')}")
        if head not in heads:
            heads.append(head)
    return heads


def carries(lineage: object) -> bool:
    """Whether a run has the snapshot, from the compare of the snapshot (base) with the run's commit (head)."""

    status = lineage.get("status") if isinstance(lineage, dict) else None
    if status in ("ahead", "identical"):
        return True
    if status in ("behind", "diverged"):
        return False
    raise Unknown(f"the compare of the snapshot with an active run's commit has status {status}")


def delivered_release(repo: str) -> str:
    """The commit of the newest deploy run on main that delivered a rollout, among its 20 newest successful runs."""

    runs = _gh(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--branch", MAIN_BRANCH,
                "--status", "success", "--limit", "20", "--json", "databaseId,headSha"])
    if not isinstance(runs, list):
        raise Unknown("the successful deploy runs are not a list")
    for run in runs:  # newest first
        if not isinstance(run, dict) or not run.get("databaseId"):
            raise Unknown("a successful deploy run has no id")
        details = _gh(["run", "view", str(run["databaseId"]), "--repo", repo, "--json", "jobs"])
        if not isinstance(details, dict) or not isinstance(details.get("jobs"), list):
            raise Unknown(f"the jobs of deploy run {run['databaseId']} are not a list")
        if delivered(details["jobs"]):
            return commit_sha(run.get("headSha"), f"the commit of delivered deploy run {run['databaseId']}")
    raise Unknown(f"none of the {len(runs)} newest successful deploy runs on main delivered a rollout")


def decide(repo: str, now: float) -> tuple[str, str]:
    """DISPATCH or WAIT, and why (see the module docstring); Unknown when an answer is missing."""

    shipped = delivered_release(repo)
    head = _gh(["api", f"repos/{repo}/commits/{MAIN_BRANCH}"])
    main_sha = commit_sha(head.get("sha") if isinstance(head, dict) else None, "main's head")
    commits = undelivered_commits(_gh(["api", f"repos/{repo}/compare/{shipped}...{main_sha}"]))
    if not commits:
        return WAIT, f"main {main_sha[:9]} is the delivered release"
    snapshot = oldest_waiting_snapshot(commits)
    if snapshot is None:
        return WAIT, f"none of the {len(commits)} commit(s) after delivered {shipped[:9]} is a price snapshot"
    snapshot_sha, committed = snapshot
    limit = SNAPSHOT_MAX_WAIT_SECONDS / 3600
    waited = f"snapshot {snapshot_sha[:9]} has waited {(now - committed) / 3600:.1f} h (limit {limit:g} h)"
    if not waited_too_long(committed, now):
        return WAIT, f"{waited}; a rollout may still carry it"
    of_head = _gh(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--commit", main_sha,
                   "--limit", "20", "--json", "databaseId,status,conclusion"])
    if not isinstance(of_head, list) or not all(isinstance(run, dict) for run in of_head):
        raise Unknown(f"the deploy runs of main's head {main_sha[:9]} are not a list of runs")
    if of_head:
        states = ", ".join(f"{run.get('databaseId')} {run.get('status')} {run.get('conclusion') or '-'}"
                           for run in of_head)
        return WAIT, (f"{waited}, and main's head {main_sha[:9]} already has a deploy run ({states}): in flight it "
                      "carries the snapshot, completed it leaves the next deploy to a person")
    active = _gh(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--branch", MAIN_BRANCH,
                  "--limit", "100", "--json", "databaseId,status,headSha,headBranch"])
    for run_head in active_main_heads(active):
        if carries(_gh(["api", f"repos/{repo}/compare/{snapshot_sha}...{run_head}"])):
            return WAIT, f"{waited}, and an active deploy run of {run_head[:9]} carries it"
    return DISPATCH, f"{waited}, and no deploy run carries it or exists for main's head {main_sha[:9]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)
    try:
        decision, why = decide(args.repo, _now())
    except Unknown as exc:
        print(f"price_snapshot_deploy: unknown: {exc}; deciding nothing", file=sys.stderr)
        return 1
    print(f"price_snapshot_deploy: {decision}: {why}", file=sys.stderr)
    print(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
