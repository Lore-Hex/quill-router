"""Freeze a prepared, green release after production admission, under one lease.

Only main-tracking runs batch successors. A train never changes its selected
commit after this step. Workflow changes form a train boundary: a controller
must not run a newer image with an older, different production workflow.
"""

from __future__ import annotations

import argparse
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


def manifest_from_zip(content: bytes) -> dict:
    require(len(content) <= MAX_ARTIFACT_BYTES, "oversized release artifact")
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            files = archive.infolist()
            require(len(files) == 1 and files[0].filename == "release.json"
                    and files[0].file_size <= MAX_MANIFEST_BYTES,
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
        require(kind in {"prepared", "selected", "public"}, "unknown release artifact")
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
        raw = manifest_from_zip(result.stdout)
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


def read_deliveries(api: GitHub, history: list[dict], head: str) -> tuple[list[str], list[Release]]:
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
                pending[pool.submit(read, listed)] = i
            if not pending:
                break
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                receipts[pending.pop(future)] = future.result()
    # Public no-downgrade floors must retain the listing order, not finish order.
    for receipt in receipts:
        if isinstance(receipt, Release):
            public_deliveries.append(receipt)
        elif receipt is not None:
            delivered.append(receipt)
    print(f"release history: checked={len(history)} api={len(delivered)} public={len(public_deliveries)} "
          f"elapsed_seconds={time.monotonic() - started:.1f}")
    return delivered, public_deliveries


def select(api: GitHub, controller: dict, base: Release, *, hotfix: bool = False) -> dict:
    require(base.commit == controller["head_sha"] and base.build_run == controller["id"],
            "base release does not belong to this workflow")
    tracking = automatic(controller)
    require(not (tracking and hotfix), "main-tracking trains require green CI")
    chosen, ci_run, deploy = base, 0 if hotfix else api.ci(base.commit), True
    if tracking:
        head = api.json("api", f"repos/{REPO}/git/ref/heads/main")["object"]["sha"]
        require(api.relation(base.commit, head) in {"ahead", "identical"}, "controller left main")
        runs = api.json("run", "list", "--repo", REPO, "--workflow", "deploy.yml", "--branch", "main",
                        "--limit", "100", "--json",
                        "databaseId,status,conclusion,headSha,headBranch,event,displayTitle")
        require(isinstance(runs, list), "release train queue unavailable")
        others = [r for r in runs if r.get("databaseId") != controller["id"]]
        delivered, public_deliveries = read_deliveries(api, [
            r for r in others if r.get("status") == "completed" and r.get("conclusion") == "success"
        ], head)
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
            "release": asdict(chosen), "ci_run": ci_run, "deploy": deploy}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "select"))
    args = parser.parse_args()
    if args.command == "prepare":
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
        release = Release.parse(raw["release"])
        with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"commit={release.commit}\nsha={release.tag}\nimage={release.image}\n")
            output.write(f"proceed={str(raw['deploy']).lower()}\n")
        print(f"release train: controller={run['head_sha']} selected={release.commit} deploy={raw['deploy']}")
    Path("release.json").write_text(json.dumps(raw, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
