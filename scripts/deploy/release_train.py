"""Freeze a prepared, green release after production admission, under one lease.

Only main-tracking runs batch successors. A train never changes its selected
commit after this step. Workflow changes form a train boundary: a controller
must not run a newer image with an older, different production workflow.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import zipfile
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from pathlib import Path
from threading import Lock
from typing import Any

from scripts.deploy import superseded_deploy as superseded

REPO = "Lore-Hex/quill-router"
IMAGE = "us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/trusted-router@sha256:"
COMPATIBILITY_PATHS = (
    ".github/workflows/deploy.yml", ".github/workflows/deploy-production.yml",
    "scripts/deploy/cloud_rollout.py", "scripts/deploy/deploy_mutex.sh",
    "scripts/deploy/release_train.py", "scripts/deploy/superseded_deploy.py",
    ".github/workflows/deploy-public-docs.yml", "scripts/deploy/public_docs.py",
    "scripts/deploy/public_surface.sh",
    "scripts/deploy/public_reuse.py",
)
FULL_SHA = re.compile(r"[a-f0-9]{40}")
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024
HISTORY_WORKERS = 4


class Refused(RuntimeError):
    pass


def require(condition: bool, reason: str) -> None:
    if not condition:
        raise Refused(reason)


@dataclass(frozen=True)
class Release:
    commit: str
    tag: str
    image: str
    build_run: int
    build_attempt: int

    @classmethod
    def parse(cls, raw: Any) -> Release:
        require(isinstance(raw, dict) and set(raw) == {
            "commit", "tag", "image", "build_run", "build_attempt",
        }, "invalid prepared release fields")
        require(isinstance(raw["commit"], str) and FULL_SHA.fullmatch(raw["commit"]) is not None,
                "invalid release commit")
        require(isinstance(raw["tag"], str) and re.fullmatch(r"[a-f0-9]{7,40}", raw["tag"]) is not None
                and raw["commit"].startswith(raw["tag"]), "release tag does not identify its commit")
        require(isinstance(raw["image"], str)
                and re.fullmatch(re.escape(IMAGE) + r"[a-f0-9]{64}", raw["image"]) is not None,
                "release image must be an immutable digest in the production repository")
        require(all(type(raw[k]) is int and raw[k] > 0 for k in ("build_run", "build_attempt")),
                "invalid build identity")
        return cls(**raw)


def manifest_from_zip(content: bytes, *, max_manifest: int = MAX_MANIFEST_BYTES) -> dict:
    require(len(content) <= MAX_ARTIFACT_BYTES, "oversized release artifact")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = archive.infolist()
            require(len(files) == 1 and files[0].filename == "release.json"
                    and files[0].file_size <= max_manifest,
                    "release artifact must contain only bounded release.json")
            raw = json.loads(archive.read(files[0]))
    except (ValueError, OSError, RuntimeError, zipfile.BadZipFile, KeyError) as exc:
        raise Refused("unreadable release artifact") from exc
    require(isinstance(raw, dict), "release manifest is not an object")
    return raw


# The ci.yml runs that can pass a release: a pull_request run checks a merge commit.
CI_GATE_EVENTS = ("push", "workflow_dispatch")


class GitHub:
    def __init__(self, deadline: float | None = None):
        self.deadline = deadline or time.monotonic() + 240
        self.cache: dict[tuple[str, ...], Any] = {}
        self.cache_lock = Lock()

    def refresh(self) -> None:
        with self.cache_lock:
            self.cache.clear()

    def json(self, *args: str) -> Any:
        with self.cache_lock:
            if args in self.cache:
                return self.cache[args]
        result = superseded._gh_json(list(args), self.deadline)
        require(result is not None, "GitHub release evidence unavailable")
        with self.cache_lock:
            return self.cache.setdefault(args, result)

    def run(self, run_id: int) -> dict:
        require(type(run_id) is int and run_id > 0, "invalid run ID")
        raw = self.json("api", f"repos/{REPO}/actions/runs/{run_id}")
        require(isinstance(raw, dict) and raw.get("id") == run_id
                and raw.get("repository", {}).get("full_name") == REPO
                and raw.get("path") == ".github/workflows/deploy.yml"
                and FULL_SHA.fullmatch(str(raw.get("head_sha", ""))) is not None,
                "unexpected release workflow provenance")
        return raw

    def relation(self, base: str, head: str) -> str:
        require(FULL_SHA.fullmatch(base) is not None and FULL_SHA.fullmatch(head) is not None,
                "invalid comparison SHA")
        if base == head:
            return "identical"
        # Full commit history (without historical blobs) comes from the
        # checkout of this repository. A positive Git proof works offline;
        # a missing/new object or an inconclusive comparison uses GitHub.
        if os.environ.get("TR_TRAIN_LOCAL_ANCESTRY") == "true":
            try:
                for ancestor, descendant, relation in ((base, head, "ahead"), (head, base, "behind")):
                    result = subprocess.run(  # noqa: S603
                        ["git", "--no-replace-objects", "merge-base", "--is-ancestor", ancestor, descendant],  # noqa: S607
                        capture_output=True, timeout=2, env={**os.environ, "GIT_NO_LAZY_FETCH": "1"},
                    )
                    if result.returncode == 0:
                        return relation
                    if result.returncode != 1:
                        break
            except (OSError, subprocess.TimeoutExpired):
                pass
        raw = self.json("api", f"repos/{REPO}/compare/{base}...{head}")
        require(isinstance(raw, dict), "invalid commit comparison")
        return str(raw.get("status", "unknown"))

    def ci(self, commit: str) -> int:
        # The newest push or dispatch run: a pull_request run checks a merge
        # commit and skips the jobs its pull request did not change.
        listed = self.json("run", "list", "--repo", REPO, "--workflow", "ci.yml", "--commit", commit,
                           "--json", "databaseId,status,conclusion,event")
        require(isinstance(listed, list), "invalid CI runs")
        rows = [row for row in listed if isinstance(row, dict) and row.get("event") in CI_GATE_EVENTS]
        require(superseded.ci_green(rows), "selected commit does not have green CI")
        require(type(rows[0].get("databaseId")) is int, "missing CI run identity")
        return rows[0]["databaseId"]

    def jobs(self, run_id: int) -> list[dict]:
        require(type(run_id) is int and run_id > 0, "invalid run ID")
        # Match `gh run view`'s latest-job listing, without its extra run and
        # workflow metadata reads for every historical release. Retained jobs
        # from an earlier attempt remain part of this listing after a rerun.
        pages = self.json("api", "--paginate", "--slurp",
                          f"repos/{REPO}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
        require(isinstance(pages, list) and bool(pages), "missing release jobs")
        rows: list[dict] = []
        total = None
        ids: set[int] = set()
        for page in pages:
            require(isinstance(page, dict) and type(page.get("total_count")) is int
                    and page["total_count"] >= 0 and isinstance(page.get("jobs"), list),
                    "invalid release jobs page")
            if total is None:
                total = page["total_count"]
            require(page["total_count"] == total, "release job count changed during pagination")
            for row in page["jobs"]:
                require(isinstance(row, dict) and type(row.get("run_id")) is int
                        and row["run_id"] == run_id and type(row.get("id")) is int
                        and row["id"] > 0 and row["id"] not in ids
                        and isinstance(row.get("name"), str) and bool(row["name"])
                        and isinstance(row.get("status"), str) and bool(row["status"])
                        and "conclusion" in row
                        and (row["conclusion"] is None or isinstance(row["conclusion"], str)),
                        "invalid release job identity or result")
                ids.add(row["id"])
                rows.append(row)
        require(len(rows) == total, "incomplete release jobs")
        return rows

    def compatible(self, base: str, candidate: str) -> bool:
        for path in COMPATIBILITY_PATHS:
            first = self.json("api", f"repos/{REPO}/contents/{path}?ref={base}")
            second = self.json("api", f"repos/{REPO}/contents/{path}?ref={candidate}")
            if (not isinstance(first, dict) or not isinstance(second, dict)
                    or not first.get("sha") or first.get("sha") != second.get("sha")):
                return False
        return True

    def artifact(self, run: dict, kind: str) -> dict:
        require(kind in {"prepared", "selected", "public", "website"}, "unknown release artifact")
        rows = self.json("api", f"repos/{REPO}/actions/runs/{run['id']}/artifacts?per_page=100")
        require(isinstance(rows, dict) and isinstance(rows.get("artifacts"), list)
                and rows.get("total_count", 101) <= 100, "incomplete release artifact listing")
        matches = []
        for item in rows["artifacts"]:
            match = re.fullmatch(kind + r"-release-([1-9][0-9]*)", str(item.get("name", "")))
            if match and int(match[1]) <= run["run_attempt"] and not item.get("expired", True):
                matches.append((int(match[1]), item))
        require(bool(matches), "prepared release artifact unavailable")
        attempt, item = max(matches, key=lambda pair: pair[0])
        require(sum(n == attempt for n, _ in matches) == 1, "ambiguous release artifact")
        require(type(item.get("id")) is int and type(item.get("size_in_bytes")) is int
                and 0 < item["size_in_bytes"] <= MAX_ARTIFACT_BYTES,
                "invalid release artifact size or ID")
        provenance = item.get("workflow_run", {})
        require(provenance.get("id") == run["id"] and provenance.get("head_sha") == run["head_sha"],
                "release artifact belongs to another run or commit")
        retries = 0
        while True:
            timeout = superseded._call_timeout(self.deadline)
            require(timeout is not None, "release selection deadline exceeded")
            try:
                result = subprocess.run(  # noqa: S603 - validated artifact ID in our fixed repository
                    ["gh", "api", f"repos/{REPO}/actions/artifacts/{item['id']}/zip"],  # noqa: S607
                    capture_output=True, check=True, timeout=timeout,
                )
                break
            except subprocess.CalledProcessError as exc:
                # The artifact is immutable, so its download is an idempotent read.
                stderr = exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else ""
                print(f"release train: artifact download failed: {stderr.strip()[:300]}", file=sys.stderr)
                if superseded._retry_transient("artifact download", stderr, retries, self.deadline):
                    retries += 1
                    continue
                raise Refused("release artifact download failed") from exc
            except (OSError, subprocess.SubprocessError) as exc:
                raise Refused("release artifact download failed") from exc
        raw = manifest_from_zip(result.stdout, max_manifest=65536 if kind == "selected" else MAX_MANIFEST_BYTES)
        require(type(raw.get("schema_version")) is int and raw["schema_version"] == 1,
                "unknown release manifest version")
        if kind == "prepared":
            release = Release.parse(raw.get("release"))
            require(release.build_run == run["id"] and release.build_attempt == attempt
                    and release.commit == run["head_sha"], "prepared release identity mismatch")
        else:
            require(raw.get("controller_run") == run["id"] and raw.get("controller_attempt") == attempt
                    and raw.get("controller_commit") == run["head_sha"], "train receipt identity mismatch")
            Release.parse(raw.get("release"))
            if kind == "public":
                release = Release.parse(raw["release"])
                require(raw.get("surface") == "public-docs" and release.commit == run["head_sha"]
                        and release.build_run == run["id"] and release.build_attempt <= attempt,
                        "invalid public-only receipt")
        return raw


def automatic(run: dict) -> bool:
    return superseded.tracks_main({"headBranch": run.get("head_branch"), "event": run.get("event"),
                                   "headSha": run.get("head_sha"), "displayTitle": run.get("display_title")})


def delivered_commit(api: GitHub, run: dict, jobs: list[dict]) -> str:
    require(superseded.delivered(jobs), "release did not complete every production gate")
    if any(j.get("name", "").removeprefix("rollout / ") == "select-release" for j in jobs):
        receipt = api.artifact(run, "selected")
        require(receipt.get("deploy") is True, "a skipped train is not a delivered release")
        release = Release.parse(receipt["release"])
        require(api.relation(run["head_sha"], release.commit) in {"ahead", "identical"},
                "train receipt does not descend from its controller")
        return release.commit
    return run["head_sha"]


def public_delivered(jobs: list[dict]) -> bool:
    public_jobs = [j for j in jobs if j.get("name") == "public-docs / deploy-public-docs"]
    require(len(public_jobs) <= 1, "ambiguous public delivery job")
    if not public_jobs or public_jobs[0].get("conclusion") != "success":
        return False
    markers = [s for s in public_jobs[0].get("steps", []) if s.get("name") == "Record public-only delivery"]
    require(len(markers) == 1 and markers[0].get("conclusion") in {"success", "skipped"},
            "public delivery outcome is unavailable")
    return markers[0]["conclusion"] == "success"


def history_key(row: dict) -> str:
    if (type(row.get("databaseId")) is not int or type(row.get("attempt")) is not int
            or row["databaseId"] <= 0 or row["attempt"] <= 0
            or not FULL_SHA.fullmatch(str(row.get("headSha", "")))
            or not re.fullmatch(r"[0-9T:.Z+-]{20,35}", str(row.get("updatedAt", "")))
            or (row.get("status"), row.get("conclusion")) != ("completed", "success")):
        return ""
    return f"{row['databaseId']}:{row['attempt']}:{row['headSha']}:{row['updatedAt']}"


def verifier_version() -> str:
    root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for path in COMPATIBILITY_PATHS:
        digest.update(hashlib.sha256((root / path).read_bytes()).digest())
    return digest.hexdigest()


def previous_history(api: GitHub, history: list[dict], head: str) -> dict:
    # Only a selected receipt from our successful main push can certify an
    # earlier scan. Never trust a branch cache, a dispatch, or an old attempt.
    candidates = (r for r in history if history_key(r) and r.get("headBranch") == "main"
                  and r.get("event") == "push")
    for _ in range(3):
        listed = next(candidates, None)
        if listed is None:
            break
        try:
            run = api.run(listed["databaseId"])
            require((run.get("head_branch"), run.get("event"), run.get("head_sha"),
                     run.get("run_attempt"), run.get("status"), run.get("conclusion")) ==
                    ("main", "push", listed["headSha"], listed["attempt"], "completed", "success")
                    and run.get("head_repository", {}).get("full_name") == REPO, "untrusted checkpoint run")
            receipt = api.artifact(run, "selected")
            checkpoint = receipt["history"]
            require(receipt["controller_attempt"] == listed["attempt"] and
                    checkpoint["verifier"] == verifier_version() and
                    api.relation(run["head_sha"], checkpoint["head"]) in {"ahead", "identical"} and
                    api.relation(checkpoint["head"], head) in {"ahead", "identical"},
                    "stale release-history checkpoint")
            entries = checkpoint["entries"]
            require(isinstance(entries, dict) and len(entries) <= 100, "invalid checkpoint size")
            parsed = {}
            for key, value in entries.items():
                if isinstance(value, dict):
                    parsed[key] = Release.parse(value)
                else:
                    require(value is None or isinstance(value, str) and FULL_SHA.fullmatch(value) is not None,
                            "invalid checkpoint delivery")
                    parsed[key] = value
            return parsed
        except (Refused, KeyError, ValueError, TypeError, AttributeError):
            continue
    return {}


def read_deliveries(api: GitHub, history: list[dict], head: str, *,
                    checkpoint: dict | None = None) -> tuple[list[str], list[Release]]:
    """Verify every completed success, before choosing a successor or refreshing evidence."""
    def read(listed: dict) -> str | Release | None:
        try:
            jobs = api.jobs(listed["databaseId"])
        except (Refused, KeyError, TypeError, ValueError):
            raise Refused("release history is incomplete") from None
        if superseded.delivered(jobs):
            run = api.run(listed["databaseId"])
            shipped = delivered_commit(api, run, jobs)
            require(api.relation(shipped, head) in {"ahead", "identical"},
                    "a delivered release is no longer on main")
            return shipped
        if public_delivered(jobs):
            run = api.run(listed["databaseId"])
            public = Release.parse(api.artifact(run, "public")["release"])
            require(api.relation(public.commit, head) in {"ahead", "identical"},
                    "a public release is no longer on main")
            return public
        return None

    cached = previous_history(api, history, head) if checkpoint is not None else {}
    delivered: list[str] = []
    public_deliveries: list[Release] = []
    started = time.monotonic()
    receipts: list[str | Release | None] = [None] * len(history)
    with ThreadPoolExecutor(max_workers=HISTORY_WORKERS) as pool:
        # Bound outstanding reads without waiting for the slowest in a batch.
        # Check every completed future before submitting any replacements.
        todo = iter(enumerate(history))
        pending = {}
        while True:
            while len(pending) < HISTORY_WORKERS:
                item = next(todo, None)
                if item is None:
                    break
                i, listed = item
                key = history_key(listed)
                if key and key in cached:
                    receipts[i] = cached[key]
                    continue
                pending[pool.submit(read, listed)] = i
            if not pending:
                break
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                receipts[pending.pop(future)] = future.result()
    # Public no-downgrade floors must retain the listing order, not finish order.
    entries = {}
    for row, receipt in zip(history, receipts, strict=True):
        if key := history_key(row):
            entries[key] = asdict(receipt) if isinstance(receipt, Release) else receipt
        if isinstance(receipt, Release):
            public_deliveries.append(receipt)
        elif receipt is not None:
            delivered.append(receipt)
    if checkpoint is not None:
        checkpoint.update(verifier=verifier_version(), head=head, entries=entries)
    print(f"release history: checked={len(history)} reused={len(entries.keys() & cached.keys())} "
          f"api={len(delivered)} public={len(public_deliveries)} "
          f"elapsed_seconds={time.monotonic() - started:.1f}")
    return delivered, public_deliveries


def select(api: GitHub, controller: dict, base: Release, *, hotfix: bool = False) -> dict:
    require(base.commit == controller["head_sha"] and base.build_run == controller["id"],
            "base release does not belong to this workflow")
    tracking = automatic(controller)
    require(not (tracking and hotfix), "main-tracking trains require green CI")
    chosen, ci_run, deploy = base, 0 if hotfix else api.ci(base.commit), True
    checkpoint: dict = {}
    if tracking:
        head = api.json("api", f"repos/{REPO}/git/ref/heads/main")["object"]["sha"]
        require(api.relation(base.commit, head) in {"ahead", "identical"}, "controller left main")
        runs = api.json("run", "list", "--repo", REPO, "--workflow", "deploy.yml", "--branch", "main",
                        "--limit", "100", "--json",
                        "databaseId,status,conclusion,headSha,headBranch,event,displayTitle,attempt,updatedAt")
        require(isinstance(runs, list), "release train queue unavailable")
        others = [r for r in runs if r.get("databaseId") != controller["id"]]
        delivered, public_deliveries = read_deliveries(api, [
            r for r in others if r.get("status") == "completed" and r.get("conclusion") == "success"
        ], head, checkpoint=checkpoint)
        for listed in others:
            try:
                if not superseded.tracks_main(listed) or listed.get("status") == "completed":
                    continue
                sha = listed["headSha"]
                if (api.relation(chosen.commit, sha) != "ahead"
                        or api.relation(sha, head) not in {"ahead", "identical"}):
                    continue
                jobs = api.jobs(listed["databaseId"])
                results = {job.get("name"): job.get("conclusion") for job in jobs}
                if not all(results.get(name) == "success" for name in ("gate-on-ci", "build-image")):
                    continue
                candidate_ci = api.ci(sha)
                if not api.compatible(base.commit, sha):
                    continue
                run = api.run(listed["databaseId"])
                candidate = Release.parse(api.artifact(run, "prepared")["release"])
                require(candidate.commit == sha, "candidate changed during selection")
                chosen, ci_run = candidate, candidate_ci
            except (Refused, KeyError, TypeError, ValueError):
                # Unknown successors never displace the verified base release.
                continue
        # A docs publication is NOT API delivery, but it is a no-downgrade
        # floor for the companion website. Reuse its prepared image only after
        # the same compatibility and CI checks as any other train candidate.
        for public in public_deliveries:
            relation = api.relation(chosen.commit, public.commit)
            require(relation in {"ahead", "identical", "behind"}, "public release history diverged")
            if relation == "ahead":
                require(api.compatible(base.commit, public.commit), "newer public release needs its own controller")
                ci_run = api.ci(public.commit)
                chosen = public
        for sha in delivered:
            relation = api.relation(chosen.commit, sha)
            require(relation in {"ahead", "identical", "behind"}, "delivered release history diverged")
            if relation in {"ahead", "identical"}:
                deploy = False
        # A final fresh lookup closes the selection window. Later commits stay
        # on the next train; force-pushed or newly red selected commits refuse.
        api.refresh()
        final_head = api.json("api", f"repos/{REPO}/git/ref/heads/main")["object"]["sha"]
        require(api.relation(chosen.commit, final_head) in {"ahead", "identical"}, "selected release left main")
        ci_run = api.ci(chosen.commit)
    return {"schema_version": 1, "controller_commit": controller["head_sha"],
            "controller_run": controller["id"], "controller_attempt": controller["run_attempt"],
            "release": asdict(chosen), "ci_run": ci_run, "deploy": deploy,
            **({"history": checkpoint} if checkpoint else {})}


class NeedsIncidentBuild(Refused):
    pass


def incident_build(api: GitHub, commit: str, *, dispatch: dict | None = None) -> tuple[Release, int]:
    """Only trust an actual production build of current main, never a tag or caller's digest."""
    require(FULL_SHA.fullmatch(commit) is not None, "invalid incident commit")
    head = api.json("api", f"repos/{REPO}/git/ref/heads/main")["object"]["sha"]
    require(head == commit, "main moved; inspect the new head before dispatching again")
    ci_run = api.ci(commit)
    listing = api.json("api", f"repos/{REPO}/actions/workflows/deploy.yml/runs"
                       f"?branch=main&event=push&head_sha={commit}&per_page=2")
    if listing.get("total_count") == 0 and listing.get("workflow_runs") == []:
        if dispatch is None:
            raise NeedsIncidentBuild("No main push image; incident dispatch will build this exact commit.")
        source = api.run(dispatch["id"])
        require(source.get("event") == "workflow_dispatch" and source.get("display_title") == f"Emergency main {commit}",
                "invalid emergency build origin")
    else:
        require(listing.get("total_count") == 1 and len(listing.get("workflow_runs", [])) == 1,
                "expected one normal main push build for this commit")
        source = api.run(listing["workflow_runs"][0]["id"])
        require(source.get("event") == "push", "invalid push build origin")
    require(source.get("head_branch") == "main"
            and source.get("head_repository", {}).get("full_name") == REPO
            and source["head_sha"] == commit and source.get("conclusion") in {None, "success"},
            "source is not an eligible main release")
    jobs = api.jobs(source["id"])
    builders = [j for j in jobs if j["name"] == "build-image"]
    require(len(builders) == 1 and builders[0].get("conclusion") == "success",
            "image build is not green; no emergency promotion")
    require(any(s.get("name") == "Build image via Cloud Build" and s.get("conclusion") == "success"
                for s in builders[0].get("steps", [])), "source job did not actually build its image")
    require(not any(j.get("conclusion") in {"failure", "cancelled", "timed_out", "action_required"}
                    for j in jobs), "source release has a failed or interrupted job")
    release = Release.parse(api.artifact(source, "prepared")["release"])
    require(release.commit == commit and release.build_run == source["id"]
            and builders[0].get("run_attempt") == release.build_attempt,
            "build job and prepared artifact do not match")
    return release, ci_run


def queued_incident(api: GitHub, head: str) -> bool:
    """Give up *unstarted* routine work only for an eligible incident already in the shared queue."""
    require(FULL_SHA.fullmatch(head) is not None, "invalid main head")
    runs = api.json("api", f"repos/{REPO}/actions/workflows/deploy.yml/runs"
                    f"?branch=main&event=workflow_dispatch&head_sha={head}&per_page=10")["workflow_runs"]
    for run in runs:
        if not (run.get("head_sha") == head and run.get("head_branch") == "main"
                and run.get("event") == "workflow_dispatch" and run.get("display_title") == f"Emergency main {head}"
                and run.get("status") in {"pending", "queued", "in_progress"}):
            continue
        jobs = api.jobs(run["id"])
        validated = any(j["name"] == "scope" and j.get("conclusion") == "success"
                        and any(s["name"] == "Validate incident promotion" and s.get("conclusion") == "success"
                                for s in j.get("steps", [])) for j in jobs)
        queued = any(j["name"] == "rollout / gate-on-ci" and j.get("status") in {"queued", "pending", "waiting"}
                     for j in jobs)
        if validated and queued:
            incident_build(api, head, dispatch=run)
            print(f"Verified queued incident: https://github.com/{REPO}/actions/runs/{run['id']}")
            return True
    return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "select", "incident-plan", "incident-preflight",
                                            "yield-to-incident"))
    parser.add_argument("--head", default="")
    args = parser.parse_args()
    incident = os.environ.get("EMERGENCY_INCIDENT", "")
    if args.command == "yield-to-incident":
        # Optional queue priority must never strand an otherwise eligible release on an API error.
        try:
            yield_slot = queued_incident(GitHub(time.monotonic() + 25), args.head)
        except (Refused, KeyError, TypeError, ValueError) as exc:
            print(f"No verified priority handoff: {exc}", file=sys.stderr)
            yield_slot = False
        raise SystemExit(0 if yield_slot else 1)
    if args.command == "incident-plan":
        api = GitHub()
        commit = api.json("api", f"repos/{REPO}/git/ref/heads/main")["object"]["sha"]
        try:
            release, ci = incident_build(api, commit)
        except NeedsIncidentBuild as exc:
            print(f"{commit}: {exc} No changes made.")
            return
        print(f"Eligible: {commit} {release.image} (build {release.build_run}, CI {ci}). No changes made.")
        return
    if args.command == "incident-preflight" or (args.command == "select" and incident):
        require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", incident) is not None
                and os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                and os.environ.get("GITHUB_REF") == "refs/heads/main"
                and os.environ.get("TR_TRAIN_HOTFIX") != "true", "invalid incident promotion context")
        api = GitHub()
        controller = api.run(int(os.environ["GITHUB_RUN_ID"]))
        require(controller["head_sha"] == os.environ["GITHUB_SHA"] and controller.get("head_branch") == "main"
                and controller.get("event") == "workflow_dispatch" and not automatic(controller),
                "incident controller must be a pinned main dispatch")
        release, ci = incident_build(api, controller["head_sha"], dispatch=controller)
        source = f"{release.build_run}/{release.build_attempt}"
        print(f"Incident {incident}: {release.commit}, {release.image}, build {source}, CI {ci}")
        if args.command == "incident-preflight":
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
                output.write(f"sha={release.tag}\nimage={release.image}\nsource={source}\n")
            return
        require((release.tag, release.image, source) == (
            os.environ["PREPARED_TAG"], os.environ["PREPARED_IMAGE"], os.environ["INCIDENT_SOURCE"],
        ), "incident build changed during admission")
        raw = {"schema_version": 1, "controller_commit": controller["head_sha"],
               "controller_run": controller["id"], "controller_attempt": controller["run_attempt"],
               "release": asdict(release), "ci_run": ci, "deploy": True, "incident": incident}
    elif args.command == "prepare":
        release = Release.parse({"commit": os.environ["GITHUB_SHA"], "tag": os.environ["PREPARED_TAG"],
                                 "image": os.environ["PREPARED_IMAGE"], "build_run": int(os.environ["GITHUB_RUN_ID"]),
                                 "build_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"])})
        raw = {"schema_version": 1, "release": asdict(release)}
    else:
        api = GitHub()
        run = api.run(int(os.environ["GITHUB_RUN_ID"]))
        require(run["head_sha"] == os.environ["GITHUB_SHA"], "controller SHA mismatch")
        base = Release.parse(api.artifact(run, "prepared")["release"])
        require(base.tag == os.environ["PREPARED_TAG"] and base.image == os.environ["PREPARED_IMAGE"],
                "prepared workflow inputs differ from their immutable manifest")
        raw = select(api, run, base, hotfix=os.environ.get("TR_TRAIN_HOTFIX") == "true")
    if args.command == "select":
        release = Release.parse(raw["release"])
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"commit={release.commit}\nsha={release.tag}\nimage={release.image}\n")
            output.write(f"proceed={str(raw['deploy']).lower()}\n")
        print(f"release train: controller={raw['controller_commit']} selected={release.commit} deploy={raw['deploy']}")
    Path("release.json").write_text(json.dumps(raw, sort_keys=True) + "\n")


if __name__ == "__main__":
    try:
        main()
    except Refused as exc:
        raise SystemExit(f"Release refused: {exc}") from exc
