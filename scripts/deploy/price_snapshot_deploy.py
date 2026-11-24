"""Does a published price snapshot need a rollout of its own?

The hourly price refresh (.github/workflows/refresh-prices.yml) pushes a
snapshot commit to main. The snapshot lives under src/, so every later
rollout of main ships it. A rollout holds the production lease for over an
hour, and a rollout for each snapshot spent several of those a day on nothing
but prices while code waited in the queue. So a snapshot rides the next
control-plane rollout, and gets one of its own only when no rollout has
carried it for SNAPSHOT_MAX_WAIT_SECONDS. The trade is deliberate: prices
reach production later, code reaches it sooner. The exception is a snapshot
that holds or removes a route (the fast path, below): it is dispatched at
once, so a failing route leaves production within about one rollout.

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
4. The oldest waiting snapshot is younger than the limit and main has no
   protective change beyond the delivered release (below): wait. A rollout
   may still carry it. A protective change takes the fast path instead.
5. A deploy.yml run of main's head has not completed: wait, that run is the
   carrier.
6. A deploy.yml run on main has not completed and its commit contains the
   oldest waiting snapshot: wait, that run carries it. A run of a commit
   from before the snapshot does not.
7. Main's head has deploy.yml runs and all of them completed: a deploy of
   exactly this head already ran and is not the delivered release (usually
   it failed or a person cancelled it; a deliberate redeploy of an older
   release after it reads the same). The check dispatches no second one,
   which bounds the automatic dispatches to one per main head, and it does
   not answer `wait` either: the snapshot is "stuck" (below) until a person
   redeploys or main moves on.
8. Otherwise dispatch.

The fast path. A protective change is one that only takes routes away, and
production should stop using a route as soon as the hold is committed, not
three to seven hours later (on 2026-11-23 a hold committed at 06:31Z was
still not live at 09:56Z while customers were routed to the failing route).
The refresh records every change to the public routes in
PROTECTIVE_HISTORY_PATH (src/trusted_router/model_changes.py), so main is
protective when the lines that file gained beyond the delivered release hold
any of:

* an endpoint or a model removed (`endpoint_removed`, `model_removed`) that
  existed in the delivered release. A route the refresh holds
  (`routable: false`, whatever its reason: provider-route-unserved,
  provider-access-unavailable, operator-credit-exhausted, a retirement) or
  drops leaves the public catalog this way. A removal of a route that was
  added after the delivered release, or a removal that a later line in the
  same range re-adds, nets to nothing and does not count;
* a `retirement_scheduled` whose effective time has come and that no later
  `schedule_cancelled` in the range cancels.

Price changes, additions and re-admissions are not protective. A range that
mixes them with a protective change is protective: the hold is the urgent
part, and the prices and additions ride along a few hours early. A range the
check cannot classify (the compare omits the file's patch, lists 300 files
or more, or a gained line is not an event) is not protective: it keeps the
four-hour rule, which is never slower than before. A hold in code
(PREPAID_PROVIDER_HOLD_REASONS) is a code commit and deploys with its own
push run.

A protective snapshot younger than the limit dispatches only when all of:

a. no deploy run of main's head exists. One in flight carries it; one that
   completed was the head's one automatic dispatch, so the snapshot waits,
   and reaches rule 7 (stuck) at the limit as before;
b. no deploy.yml run on main is in flight, whatever its commit. This is the
   rate limit: a run of hourly protective snapshots coalesces behind the
   rollout in flight, and the first check after it ends dispatches one
   rollout for all of them (or nothing, if that rollout delivered them). The
   deploy mutex and its FIFO lease order are untouched: at most one
   automatic snapshot rollout is ever queued;
c. main's head has a completed, successful ci.yml run (the newest push or
   workflow_dispatch run of that exact commit, the run deploy.yml's
   gate-on-ci reads). A dispatch the gate would refuse would spend the
   head's one dispatch, so the check waits for green instead. The gate
   itself still runs in the dispatched deploy and is never bypassed.

Otherwise it waits, naming which of them held it.

Every answer the rule needs and cannot get is "unknown": a gh call that
fails GH_ATTEMPTS times in a row, a commit id that is not 40 hex digits, a
main that is not ahead of the delivered release, a commit list the API
truncated, a snapshot without a readable committer date, a run list of an
unexpected shape. Then the reason goes to stderr, nothing goes to stdout and
the exit code is 1. The workflow step fails where a person sees it, and the
next refresh run asks again. The check never guesses: a guessed `dispatch`
spends a rollout, and a guessed `wait` hides a snapshot that is stuck.

"Stuck" (rule 7) ends the same way, with its own word in the reason: nothing
on stdout, exit code 1, a failed step every hour. A quiet `wait` there would
leave the snapshot waiting without a bound and without anyone being told. It
clears by itself when main's head changes: the new head has no deploy run, so
the next check waits for its push run or dispatches.
"""

from __future__ import annotations

import argparse
import json
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
# The committed history of public route changes (src/trusted_router/model_changes.py).
PROTECTIVE_HISTORY_PATH = "src/trusted_router/data/model_changes.jsonl"
# History events that take a route away, and the ones that bring it back.
REMOVAL_TYPES = frozenset({"endpoint_removed", "model_removed"})
ADDITION_TYPES = frozenset({"endpoint_added", "model_added"})
# The compare API lists at most this many files; a list that long may lack the history.
COMPARE_FILE_LIMIT = 300
CI_WORKFLOW = "ci.yml"
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


class Stuck(Exception):
    """A snapshot is past the limit and the check may not dispatch for it; the message says why."""


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


def protective_changes(compare: dict, now: float) -> tuple[list[str] | None, str]:
    """The route removals main has beyond the delivered release, from the compare's files.

    Returns (changes, note): changes is None when the range cannot be
    classified, and the note says why; an empty list means not protective.
    """

    files = compare.get("files")
    if not isinstance(files, list) or not all(isinstance(entry, dict) for entry in files):
        return None, "the compare lists no files"
    history = [entry for entry in files if entry.get("filename") == PROTECTIVE_HISTORY_PATH]
    if not history:
        if len(files) >= COMPARE_FILE_LIMIT:
            return None, f"the compare lists {len(files)} files and not the route history"
        return [], "no route history change"
    patch = history[0].get("patch")
    if not isinstance(patch, str):
        return None, "the compare omits the route history's patch"
    gained: list[dict] = []
    dropped: set[str] = set()
    for line in patch.splitlines():
        if line.startswith(("+++", "---")) or not line.startswith(("+", "-")) or not line[1:].strip():
            continue
        try:
            event = json.loads(line[1:])
        except ValueError:
            return None, "a changed route history line is not JSON"
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            return None, "a changed route history line is not an event"
        if line.startswith("-"):
            dropped.add(str(event.get("id")))
        else:
            gained.append(event)
    # A line the range only moved (dropped and gained again) is not a new event.
    gained = [event for event in gained if str(event.get("id")) not in dropped]
    gained.sort(key=lambda event: str(event.get("recorded_at") or event.get("effective_at") or ""))
    first: dict[tuple[str, str], str] = {}
    last: dict[tuple[str, str], str] = {}
    cancelled = {str(event["before"].get("id")) for event in gained
                 if event["type"] == "schedule_cancelled" and isinstance(event.get("before"), dict)}
    changes: list[str] = []
    for event in gained:
        kind = event["type"]
        if kind in REMOVAL_TYPES or kind in ADDITION_TYPES:
            route = (str(event.get("model")), str(event.get("endpoint") or ""))
            first.setdefault(route, kind)
            last[route] = kind
        elif kind == "retirement_scheduled" and str(event.get("id")) not in cancelled:
            effective = _epoch(event.get("effective_at"))
            if effective is not None and effective <= now:
                changes.append(f"retirement of {event.get('model')} in effect")
    for route, kind in last.items():
        if kind in REMOVAL_TYPES and first[route] not in ADDITION_TYPES:
            model, endpoint = route
            changes.append(f"{endpoint or model} removed")
    return sorted(changes), f"{len(gained)} route history event(s)"


def ci_green(runs: object, sha: str) -> tuple[bool, str]:
    """Whether the run deploy.yml's gate-on-ci reads for SHA (the newest push or dispatch ci.yml run) is green."""

    if not isinstance(runs, list) or not all(isinstance(run, dict) for run in runs):
        raise Unknown(f"the ci.yml runs of {sha[:9]} are not a list of runs")
    gated = [run for run in runs if run.get("event") in ("push", "workflow_dispatch")]
    if not gated:
        return False, f"main's head {sha[:9]} has no ci.yml run yet"
    run = gated[0]  # newest first, as the gate reads it
    state = f"ci.yml run {run.get('databaseId')} of main's head {sha[:9]} is {run.get('status')} " \
            f"{run.get('conclusion') or '-'}"
    return run.get("status") == "completed" and run.get("conclusion") == "success", state


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


def runs_of_head(repo: str, main_sha: str) -> tuple[list[dict], str]:
    """The deploy runs of main's head, each with a status, and a summary of their states."""

    of_head = _gh(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--commit", main_sha,
                   "--limit", "20", "--json", "databaseId,status,conclusion"])
    if not isinstance(of_head, list) or not all(isinstance(run, dict) for run in of_head):
        raise Unknown(f"the deploy runs of main's head {main_sha[:9]} are not a list of runs")
    if not all(isinstance(run.get("status"), str) and run["status"] for run in of_head):
        raise Unknown(f"a deploy run of main's head {main_sha[:9]} has no status")
    states = ", ".join(f"{run.get('databaseId')} {run['status']} {run.get('conclusion') or '-'}" for run in of_head)
    return of_head, states


def active_deploy_runs(repo: str) -> object:
    return _gh(["run", "list", "--repo", repo, "--workflow", DEPLOY_WORKFLOW, "--branch", MAIN_BRANCH,
                "--limit", "100", "--json", "databaseId,status,headSha,headBranch"])


def fast_path(repo: str, main_sha: str, waited: str, changes: list[str] | None, note: str) -> tuple[str, str]:
    """A snapshot younger than the limit: DISPATCH only for a protective change and when (a)-(c) allow it."""

    if changes is None:
        return WAIT, f"{waited}; cannot classify it as protective ({note}), so a rollout may still carry it"
    if not changes:
        return WAIT, f"{waited}; not protective ({note}), so a rollout may still carry it"
    shown = ", ".join(changes[:5]) + (f" and {len(changes) - 5} more" if len(changes) > 5 else "")
    protective = f"{waited}; protective ({shown})"
    of_head, states = runs_of_head(repo, main_sha)
    if any(run["status"] != "completed" for run in of_head):
        return WAIT, f"{protective}, and a deploy run of main's head {main_sha[:9]} is in flight ({states}): it carries it"
    if of_head:
        return WAIT, (f"{protective}, but main's head {main_sha[:9]} already had its one automatic deploy "
                      f"({states}); the snapshot waits for a new head or the limit")
    in_flight = active_main_heads(active_deploy_runs(repo))
    if in_flight:
        return WAIT, (f"{protective}, and deploy run(s) of {', '.join(head[:9] for head in in_flight)} are in "
                      "flight: it coalesces into the next rollout")
    green, ci_state = ci_green(_gh(["run", "list", "--repo", repo, "--workflow", CI_WORKFLOW, "--commit", main_sha,
                                    "--limit", "20", "--json", "databaseId,status,conclusion,event"]), main_sha)
    if not green:
        return WAIT, f"{protective}, but {ci_state}: no dispatch before CI is green on the same commit"
    return DISPATCH, f"{protective}; fast path: no deploy run in flight and {ci_state}"


def decide(repo: str, now: float) -> tuple[str, str]:
    """DISPATCH or WAIT, and why (see the module docstring).

    Unknown when an answer is missing; Stuck when the snapshot is past the limit
    and main's head already had its one deploy.
    """

    shipped = delivered_release(repo)
    head = _gh(["api", f"repos/{repo}/commits/{MAIN_BRANCH}"])
    main_sha = commit_sha(head.get("sha") if isinstance(head, dict) else None, "main's head")
    compare = _gh(["api", f"repos/{repo}/compare/{shipped}...{main_sha}"])
    commits = undelivered_commits(compare)
    if not commits:
        return WAIT, f"main {main_sha[:9]} is the delivered release"
    snapshot = oldest_waiting_snapshot(commits)
    if snapshot is None:
        return WAIT, f"none of the {len(commits)} commit(s) after delivered {shipped[:9]} is a price snapshot"
    snapshot_sha, committed = snapshot
    limit = SNAPSHOT_MAX_WAIT_SECONDS / 3600
    waited = f"snapshot {snapshot_sha[:9]} has waited {(now - committed) / 3600:.1f} h (limit {limit:g} h)"
    if not waited_too_long(committed, now):
        if not isinstance(compare, dict):  # undelivered_commits already refused anything else
            raise Unknown("the compare of the delivered release with main is not an object")
        return fast_path(repo, main_sha, waited, *protective_changes(compare, now))
    of_head, states = runs_of_head(repo, main_sha)
    if any(run["status"] != "completed" for run in of_head):
        return WAIT, (f"{waited}, and a deploy run of main's head {main_sha[:9]} is in flight ({states}): "
                      "it carries the snapshot")
    for run_head in active_main_heads(active_deploy_runs(repo)):
        if carries(_gh(["api", f"repos/{repo}/compare/{snapshot_sha}...{run_head}"])):
            return WAIT, f"{waited}, and an active deploy run of {run_head[:9]} carries it"
    if of_head:
        raise Stuck(f"{waited}, no active deploy run carries it, and every deploy run of main's head "
                    f"{main_sha[:9]} completed ({states}) without becoming the delivered release: one automatic "
                    "dispatch per head, so a person redeploys")
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
    except Stuck as exc:
        print(f"price_snapshot_deploy: stuck: {exc}", file=sys.stderr)
        return 1
    print(f"price_snapshot_deploy: {decision}: {why}", file=sys.stderr)
    print(decision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
