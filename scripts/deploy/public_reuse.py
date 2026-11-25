"""Keep an independently verified website revision for isolated worker changes."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from scripts.deploy import public_docs as docs
from scripts.deploy.public_rollout_report import signature
from scripts.deploy.release_train import IMAGE, REPO, GitHub, Refused, Release, require

# Standalone job entry points, not shared storage, pricing, or serving code.
WORKERS = {f"src/trusted_router/{name}_cli.py" for name in (
    "trust_reconcile", "owner_inventory", "receipt_key_backfill", "activity_delivery_repair",
    "provider_benchmark_backfill", "trust_tier", "trust_backfill",
)}
SECRETS = {"trustedrouter-attribution-cookie-key", "trustedrouter-sentry-dsn",
           "trustedrouter-clickhouse-control-read-password"}
FAILURES = (Refused, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError)


def worker_delta(base: str, target: str) -> bool:
    base, target = docs.commit(base), docs.commit(target)
    if base == target or docs.git("merge-base", base, target) != base:
        return False
    raw = docs.command("git", "diff", "--raw", "--no-renames", "-z", base, target).split("\0")
    require(raw.pop() == "" and len(raw) % 2 == 0, "malformed diff")
    touched = False
    for metadata, path in zip(raw[::2], raw[1::2], strict=True):
        before, after, _, _, status = metadata.split()
        if path in WORKERS and (before, after, status) == (":100644", "100644", "M"):
            touched = True
        elif not (path.startswith(("docs/", "tests/")) and before in {":100644", ":000000"}
                  and after in {"100644", "000000"} and status in {"A", "D", "M"}):
            return False
    return touched


def fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def runtime_layers(config: dict) -> tuple[list[str], dict]:
    # Compare the actual OS, uv, apt and installed dependencies, not just
    # Dockerfile/uv.lock. A floating base rebuilt with security fixes matters.
    history = [h for h in config["history"] if not h.get("empty_layer")]
    layers = config["rootfs"]["diff_ids"]
    require(len(history) == len(layers) and all(isinstance(layer, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", layer)
                                               for layer in layers), "unrecognized image layers")
    boundaries = [i for i, h in enumerate(history)
                  if h["created_by"].strip() in {"COPY src ./src # buildkit", "COPY src ./src"}
                  or re.fullmatch(r"/bin/sh -c #\(nop\)\s+COPY dir:[0-9a-f]{64} in \./src\s*",
                                  h["created_by"])]
    require(len(boundaries) == 1 and boundaries[0] > 0, "unrecognized public image build")
    return layers[:boundaries[0]], config["config"]


def runtime(image: str) -> tuple[list[str], dict]:
    require(re.fullmatch(re.escape(IMAGE) + r"[0-9a-f]{64}", image) is not None, "unexpected registry")
    return runtime_layers(json.loads(docs.command("docker", "buildx", "imagetools", "inspect", "--format",
                                                  "{{json .Image}}", image, timeout=15)))


def active(region: str, service: str) -> tuple[dict, dict]:
    live = docs.cloud("run", "services", "describe", service, "--region", region)
    traffic, = live["status"]["traffic"]
    name = traffic["revisionName"]
    require(name.startswith(service + "-") and traffic["percent"] == 100 and not traffic.get("tag")
            and name == live["status"]["latestReadyRevisionName"] == live["status"]["latestCreatedRevisionName"],
            "fleet not converged")
    revision = docs.cloud("run", "revisions", "describe", name, "--region", region)
    require(revision["metadata"]["name"] == name, "unexpected revision")
    return live, revision


def inventory(target: str, image: str) -> dict:
    api, website = docs.SERVICES
    _, primary = active(docs.REGIONS[0], api)
    container, = primary["spec"]["containers"]
    env = {e["name"]: e for e in container["env"]}
    require(docs.commit(env.pop("TR_RELEASE")["value"]) == target and container["image"] == image,
            "API has not reached the selected release")
    versions = {s: docs.cloud("secrets", "versions", "describe", "latest", "--secret", s) for s in SECRETS}
    require(all(v["state"] == "ENABLED" and v["name"].rsplit("/", 1)[1].isdigit()
                for v in versions.values()), "secret version not enabled")

    def region_evidence(region: str) -> dict:
        service, revision = active(region, website)
        current = signature(service, revision)
        created = datetime.fromisoformat(revision["metadata"]["creationTimestamp"])
        require(datetime.now(UTC) - timedelta(days=1) <= created <= datetime.now(UTC),
                "website is due for full refresh")
        require(all(datetime.fromisoformat(v["createTime"]) <= created for v in versions.values()),
                "secret rotated since deployment")
        refs = [e["valueFrom"]["secretKeyRef"] for e in revision["spec"]["containers"][0]["env"] if "valueFrom" in e]
        require(len(refs) == len(SECRETS) and {r["name"] for r in refs} == SECRETS
                and all(r["key"] == "latest" for r in refs), "unexpected secret bindings")
        policy = docs.cloud("run", "services", "get-iam-policy", website, "--region", region)
        require(any(b["role"] == "roles/run.invoker" and "allUsers" in b["members"] and "condition" not in b
                    for b in policy["bindings"]), "public invoker not verified")
        return {**current, "revision": revision["metadata"]["name"], "iam": fingerprint(policy)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        fleet = dict(zip(docs.REGIONS, pool.map(region_evidence, docs.REGIONS), strict=True))
    return {"fleet": fleet, "api_configuration": fingerprint(env),
            "secrets": fingerprint({k: v["name"] for k, v in versions.items()})}


def baseline(target: str) -> dict:
    api = GitHub(time.monotonic() + 45)
    rows = api.json("run", "list", "--repo", REPO, "--workflow", "deploy.yml", "--branch", "main",
                    "--event", "push", "--status", "success", "--limit", "6", "--json", "databaseId")
    for row in rows:
        try:
            run = api.run(row["databaseId"])
            require(run["event"] == "push" and run["head_branch"] == "main" and run["conclusion"] == "success"
                    and run["head_repository"]["full_name"] == REPO, "not a successful main push")
            receipt = api.artifact(run, "website")
            require(receipt["controller_attempt"] == run["run_attempt"] and receipt["site_format"] == 1,
                    "stale website receipt")
            selected = api.artifact(run, "selected")
            require(selected["deploy"] is True and selected["release"] == receipt["release"], "release mismatch")
            release = Release.parse(receipt["release"])
            if worker_delta(release.commit, target):
                return receipt["inventory"]
        except FAILURES:
            continue
    raise Refused("no qualifying full publication receipt")


def worker_only(target: str, image: str) -> bool:
    if not worker_delta(docs.git("rev-parse", "HEAD^1"), target):
        return False
    previous = baseline(target)
    if inventory(target, image) != previous:
        return False
    old_images = {r["image"] for r in previous["fleet"].values()}
    require(len(old_images) == 1, "mixed website images")
    docs.command("gcloud", "auth", "configure-docker", "us-central1-docker.pkg.dev", "--quiet", timeout=15)
    if runtime(old_images.pop()) != runtime(image):
        return False
    # This URL must now report the new API revision while the old site still
    # serves. An unapplied edge change or stale trust document forces rollout.
    live = json.loads(docs.command("curl", "-fsS", "--max-time", "10",
                                  "https://trustedrouter.com/trust/control-plane.json"))
    return live["plane"] == "trustedrouter.com" and docs.commit(live["release"]) == target


def main() -> None:
    if os.environ.get("GITHUB_EVENT_NAME") != "push" or os.environ.get("GITHUB_REF") != "refs/heads/main":
        return
    target, image = os.environ["EXPECTED_COMMIT"], os.environ["EXPECTED_IMAGE"]
    require(target == docs.git("rev-parse", "HEAD"), "wrong source")
    if sys.argv[1] == "plan":
        if worker_only(target, image):
            with Path(os.environ["GITHUB_OUTPUT"]).open("a") as f:
                print("reuse=true", file=f)
            print("Keeping the previously verified website in all four regions.")
        return
    require(sys.argv[1] == "record", "unknown command")
    run_id = int(os.environ["GITHUB_RUN_ID"])
    api = GitHub()
    run = api.run(run_id)
    selected = api.artifact(run, "selected")
    release = Release.parse(selected["release"])
    require(selected["deploy"] is True and (release.commit, release.image) == (target, image), "wrong receipt")
    observed = inventory(target, image)
    require(all(r["image"] == image and r["api_source"] == r["website_source"] == target
                for r in observed["fleet"].values()), "website rollout did not converge")
    receipt = {"schema_version": 1, "site_format": 1, "controller_run": run_id,
               "controller_commit": run["head_sha"], "controller_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
               "release": selected["release"], "inventory": observed}
    path = Path("website-receipt/release.json")
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(receipt, sort_keys=True) + "\n")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        print("certified=true", file=output)


if __name__ == "__main__":
    try:
        main()
    except FAILURES as exc:
        # Metadata is optional, not authority to skip. No output means full rollout.
        print(f"Website reuse not proven ({type(exc).__name__}); using ordinary publication.", file=sys.stderr)
