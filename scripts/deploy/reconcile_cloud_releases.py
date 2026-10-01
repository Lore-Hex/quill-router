#!/usr/bin/env python3
"""Dispatch stale secondary control planes to the last fully verified GCP release."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any

from scripts.deploy.cloud_rollout import Refused, probe_cloud

REPO = "Lore-Hex/quill-router"
REQUIRED_JOBS = {"deploy", "rollout-secondaries", "public-surface-companion",
                 "verify-cloud-complete", "finalize-cloud"}


def gh(*args: str) -> Any:
    result = subprocess.run(  # noqa: S603,S607 - fixed CLI/repository; no shell
        ["gh", *args], capture_output=True, text=True, check=True, timeout=60,  # noqa: S607
    )
    return json.loads(result.stdout) if result.stdout.strip() else None


def completed_release() -> str:
    runs = gh("run", "list", "--repo", REPO, "--workflow", "deploy.yml", "--branch", "main",
              "--status", "success", "--limit", "30", "--json", "databaseId,headSha")
    for run in runs:
        details = gh("run", "view", str(run["databaseId"]), "--repo", REPO, "--json", "jobs")
        jobs = {job["name"]: job["conclusion"] for job in details["jobs"]}
        if all(jobs.get(name) == "success" for name in REQUIRED_JOBS):
            sha = run["headSha"]
            if not re.fullmatch(r"[a-f0-9]{40}", sha):
                raise Refused("invalid completed release SHA")
            return sha
    raise Refused("no fully verified coordinated GCP release; do not dispatch older unguarded workflows")


def verify_promotion(cloud: str, candidate: str) -> bool:
    """Recheck under the cloud reservation, not just at dispatch time."""
    if cloud not in {"aws", "azure"} or not re.fullmatch(r"[a-f0-9]{40}", candidate):
        raise Refused("invalid promotion target")
    serving = probe_cloud(cloud)
    comparison = gh("api", f"repos/{REPO}/compare/{serving}...{candidate}")
    if comparison["status"] in {"behind", "identical"}:
        return False
    if comparison["status"] != "ahead":
        raise Refused("queued promotion would downgrade or diverge from the current release")
    return True


def reconcile() -> list[dict[str, str]]:
    candidate = completed_release()
    main = gh("api", f"repos/{REPO}/compare/{candidate}...main")
    if main["status"] not in {"ahead", "identical"}:
        raise Refused("verified release is no longer on main")
    gcp_release = probe_cloud("gcp")
    if gh("api", f"repos/{REPO}/compare/{gcp_release}...{candidate}")["status"] != "identical":
        raise Refused("GCP no longer serves the completed candidate; wait for rollout or investigate rollback")
    result = []
    for cloud in ("aws", "azure"):
        workflow = f"deploy-{cloud}-control-plane.yml"
        runs = gh("run", "list", "--repo", REPO, "--workflow", workflow, "--limit", "100",
                  "--json", "status")
        if any(run["status"] != "completed" for run in runs):
            result.append({"cloud": cloud, "action": "already queued or running"})
            continue
        serving = probe_cloud(cloud)
        # Compare exact Git commit identities, not timestamps or short-SHA order.
        comparison = gh("api", f"repos/{REPO}/compare/{serving}...{candidate}")
        if comparison["status"] in {"identical", "behind"}:
            result.append({"cloud": cloud, "action": "current or newer"})
            continue
        if comparison["status"] != "ahead":
            raise Refused(f"{cloud} release diverges from verified candidate; refusing rollback")
        args = ["workflow", "run", workflow, "--repo", REPO, "--ref", "main",
                "-f", f"release_sha={candidate}", "-f", "promotion_only=true"]
        if cloud == "aws":
            args.extend(["-f", "mode=deploy"])
        gh(*args)
        result.append({"cloud": cloud, "action": "dispatched", "release": candidate})
    return result


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "verify-promotion":
        raise SystemExit(0 if verify_promotion(sys.argv[2], sys.argv[3]) else 75)
    elif len(sys.argv) == 1:
        print(json.dumps(reconcile(), indent=2))
    else:
        raise SystemExit("usage: reconcile_cloud_releases [verify-promotion CLOUD SHA]")
