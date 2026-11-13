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
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from scripts.deploy import superseded_deploy as superseded

REPO = "Lore-Hex/quill-router"
IMAGE = "us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/trusted-router@sha256:"
COMPATIBILITY_PATHS = (
    ".github/workflows/deploy.yml", ".github/workflows/deploy-production.yml",
    "scripts/deploy/cloud_rollout.py", "scripts/deploy/deploy_mutex.sh",
    "scripts/deploy/release_train.py", "scripts/deploy/superseded_deploy.py",
)
FULL_SHA = re.compile(r"[a-f0-9]{40}")
MAX_ARTIFACT_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024


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


class GitHub:
    def __init__(self, deadline: float | None = None):
        self.deadline = deadline or time.monotonic() + 240
        self.cache: dict[tuple[str, ...], Any] = {}

    def refresh(self) -> None:
        self.cache.clear()

    def json(self, *args: str) -> Any:
        if args not in self.cache:
            result = superseded._gh_json(list(args), self.deadline)
            require(result is not None, "GitHub release evidence unavailable")
            self.cache[args] = result
        return self.cache[args]

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
        rows = self.json("run", "list", "--repo", REPO, "--workflow", "ci.yml", "--commit", commit,
                         "--limit", "1", "--json", "databaseId,status,conclusion")
        require(superseded.ci_green(rows), "selected commit does not have green CI")
        require(type(rows[0].get("databaseId")) is int, "missing CI run identity")
        return rows[0]["databaseId"]

    def jobs(self, run_id: int) -> list[dict]:
        raw = self.json("run", "view", str(run_id), "--repo", REPO, "--json", "jobs")
        require(isinstance(raw, dict) and isinstance(raw.get("jobs"), list), "missing release jobs")
        return raw["jobs"]

    def compatible(self, base: str, candidate: str) -> bool:
        for path in COMPATIBILITY_PATHS:
            first = self.json("api", f"repos/{REPO}/contents/{path}?ref={base}")
            second = self.json("api", f"repos/{REPO}/contents/{path}?ref={candidate}")
            if (not isinstance(first, dict) or not isinstance(second, dict)
                    or not first.get("sha") or first.get("sha") != second.get("sha")):
                return False
        return True

    def artifact(self, run: dict, kind: str) -> dict:
        require(kind in {"prepared", "selected"}, "unknown release artifact")
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
        timeout = superseded._call_timeout(self.deadline)
        require(timeout is not None, "release selection deadline exceeded")
        try:
            result = subprocess.run(  # noqa: S603 - validated artifact ID in our fixed repository
                ["gh", "api", f"repos/{REPO}/actions/artifacts/{item['id']}/zip"],  # noqa: S607
                capture_output=True, check=True, timeout=timeout,
            )
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
        delivered = []
        for listed in runs:
            if listed.get("databaseId") == controller["id"]:
                continue
            if listed.get("status") == "completed" and listed.get("conclusion") == "success":
                # History is fail-closed; unknown successors, unlike unknown
                # deliveries, can simply wait. Ignore cancelled/failed builds.
                try:
                    jobs = api.jobs(listed["databaseId"])
                except (Refused, KeyError, TypeError, ValueError):
                    raise Refused("release history is incomplete") from None
                if superseded.delivered(jobs):
                    run = api.run(listed["databaseId"])
                    shipped = delivered_commit(api, run, jobs)
                    require(api.relation(shipped, head) in {"ahead", "identical"},
                            "a delivered release is no longer on main")
                    delivered.append(shipped)
                continue
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
