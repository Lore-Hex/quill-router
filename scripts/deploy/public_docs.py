"""Publish reviewed website inputs without rolling the API.

CI/build are not shortened. Unknown changes use the ordinary release train;
unreadable serving evidence fails closed. A public receipt is never API delivery.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from dataclasses import asdict
from pathlib import Path
from typing import Any

from scripts.deploy.release_train import GitHub, Release, require

DOC = "src/trusted_router/templates/public/docs.html"
# Existing public files only. Do not include shared templates, scripts, route
# registries or the Python blog catalog: the combined service also loads them.
SITE_FILES = frozenset({
    DOC, "src/trusted_router/templates/public/about.html",
    "src/trusted_router/templates/public/blog_index.html",
    "src/trusted_router/templates/homepage/content.json",
    "src/trusted_router/static/homepage/application.css",
    "src/trusted_router/static/homepage/components.css",
})


def site_file(path: str) -> bool:
    return path in SITE_FILES or re.fullmatch(
        r"src/trusted_router/templates/homepage/[a-z][a-z0-9_]*\.html", path
    ) is not None


REGIONS = ("us-central1", "us-east4", "europe-west4", "southamerica-east1")
PROJECT = "quill-cloud-proxy"
SERVICES = ("trusted-router", "trusted-router-public")
MANIFEST = Path("public-docs-input.json")


def command(*args: str, timeout: int = 120, env: dict[str, str] | None = None) -> str:
    # Never include captured cloud output in exceptions: revision env can be sensitive.
    result = subprocess.run(args, capture_output=True, timeout=timeout, env=env)  # noqa: S603
    require(result.returncode == 0, f"{args[0]} command failed")
    return result.stdout.decode()


def git(*args: str) -> str:
    return command("git", *args).strip()


def commit(value: str) -> str:
    require(re.fullmatch(r"[a-f0-9]{7,40}", value) is not None, "invalid serving source")
    resolved = git("rev-parse", "--verify", value + "^{commit}")
    require(re.fullmatch(r"[a-f0-9]{40}", resolved) is not None, "ambiguous serving source")
    return resolved


def docs_delta(base: str, head: str) -> bool:
    """Allow only existing reviewed site files and non-image tests/docs."""
    base, head = commit(base), commit(head)
    if git("merge-base", base, head) != base:
        return False
    raw = command("git", "diff", "--raw", "--no-abbrev", "--no-renames", "-z", base, head)
    fields = raw.split("\0")
    require(fields.pop() == "" and len(fields) % 2 == 0, "invalid raw diff")
    for metadata, path in zip(fields[::2], fields[1::2], strict=True):
        bits = metadata.split()
        require(len(bits) == 5 and bits[0].startswith(":"), "invalid raw diff entry")
        old_mode, new_mode = bits[0][1:], bits[1]
        if (old_mode not in {"100644", "000000"} or new_mode not in {"100644", "000000"}
                or bits[4] not in {"A", "M", "D"}):
            return False
        if site_file(path):
            if bits[4] != "M" or old_mode != new_mode:
                return False
        elif not path.startswith(("tests/", "docs/")):
            return False
    return True


def proposed_docs_only() -> bool:
    head = git("rev-parse", "HEAD")
    parent = git("rev-parse", "HEAD^1")
    return docs_delta(parent, head) and any(
        site_file(p) for p in command("git", "diff", "--name-only", "-z", parent, head).split("\0")
    )


def cloud(*args: str) -> Any:
    return json.loads(command("gcloud", "--project", PROJECT, *args, "--format=json"))


def serving(region: str, service: str) -> dict[str, str]:
    current = cloud("run", "services", "describe", service, "--region", region)
    traffic = [t for t in current["status"]["traffic"] if t.get("percent", 0) > 0]
    require(len(traffic) == 1 and traffic[0].get("percent") == 100,
            "serving traffic is not a single 100% revision")
    name = traffic[0].get("revisionName", "")
    require(name.startswith(service + "-"), "serving revision is missing")
    require(current["metadata"]["annotations"].get("run.googleapis.com/ingress")
            == "internal-and-cloud-load-balancing", "serving origin is not LB-only")
    revision = cloud("run", "revisions", "describe", name, "--region", region)
    require(revision["metadata"]["name"] == name, "serving revision identity changed")
    ready = [c for c in revision["status"]["conditions"] if c.get("type") == "Ready"]
    require(len(ready) == 1 and ready[0].get("status") == "True", "serving revision is not ready")
    containers = revision["spec"]["containers"]
    require(len(containers) == 1, "unexpected serving containers")
    image = containers[0]["image"]
    require(re.fullmatch(r"us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/"
                         r"trusted-router@sha256:[a-f0-9]{64}", image) is not None,
            "serving image is not immutable")
    env = [e for e in containers[0]["env"] if e.get("name") == "TR_RELEASE"]
    require(len(env) == 1 and isinstance(env[0].get("value"), str), "missing serving source")
    api_commit = commit(env[0]["value"])
    public = [e for e in containers[0]["env"] if e.get("name") == "TR_PUBLIC_SOURCE_COMMIT"]
    require(len(public) <= 1, "ambiguous public source")
    source = commit(public[0]["value"]) if service == SERVICES[1] and public else api_commit
    return {"region": region, "service": service, "revision": name, "image": image,
            "commit": source, "api_commit": api_commit,
            "spec_sha256": hashlib.sha256(json.dumps(revision["spec"], sort_keys=True).encode()).hexdigest()}


def inventory() -> list[dict[str, str]]:
    return [serving(region, service) for service in SERVICES for region in REGIONS]


def eligible(rows: list[dict[str, str]], release: Release) -> bool:
    require({(r["service"], r["region"]) for r in rows}
            == {(s, r) for s in SERVICES for r in REGIONS} and len(rows) == 8,
            "incomplete serving inventory")
    sources = {row[key] for row in rows for key in ("commit", "api_commit")}
    return all(docs_delta(source, release.commit) for source in sorted(sources))


def validated_release() -> Release:
    require(os.environ.get("GITHUB_REPOSITORY") == "Lore-Hex/quill-router"
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and (os.environ.get("GITHUB_EVENT_NAME") == "push" or
                 (os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
                  and os.environ.get("TR_WEBSITE_ONLY") == "true")),
            "website publication requires a main push or explicit website-only dispatch")
    api = GitHub()
    run = api.run(int(os.environ["GITHUB_RUN_ID"]))
    release = Release.parse(api.artifact(run, "prepared")["release"])
    require(release.commit == os.environ["GITHUB_SHA"] == git("rev-parse", "HEAD")
            and release.tag == os.environ["PREPARED_TAG"]
            and release.image == os.environ["PREPARED_IMAGE"], "prepared docs identity mismatch")
    require(proposed_docs_only(), "controller is not docs-only")
    api.ci(release.commit)
    head = api.json("api", "repos/Lore-Hex/quill-router/git/ref/heads/main")["object"]["sha"]
    require(api.relation(release.commit, head) in {"ahead", "identical"}, "docs commit left main")
    return release


def candidate() -> None:
    """Recheck the actual input and all eight sources immediately before mutation."""
    release = validated_release()
    require(os.environ.get("TR_PUBLIC_DOCS_MANIFEST") == str(MANIFEST), "unexpected docs input path")
    recorded = json.loads(MANIFEST.read_text())
    require(recorded["release"] == asdict(release), "docs input changed")
    current = inventory()
    require(current == recorded["before"] and eligible(current, release), "serving docs baseline changed")
    print(release.commit, release.image)


def guard_source(source: str, target: str) -> None:
    """Even a manual companion or a failed receipt upload cannot undo docs."""
    source, target = commit(source), commit(target)
    require(git("merge-base", source, target) == source,
            "rollout would downgrade or diverge from the public website")


def verify(before: list[dict[str, str]], release: Release) -> list[dict[str, str]]:
    after = inventory()
    require([r for r in after if r["service"] == SERVICES[0]]
            == [r for r in before if r["service"] == SERVICES[0]], "API runtime changed during docs publication")
    require(all(r["commit"] == release.commit and r["image"] == release.image
                for r in after if r["service"] == SERVICES[1]), "public fleet did not converge")
    primary_api = next(r["api_commit"] for r in before
                       if r["service"] == SERVICES[0] and r["region"] == REGIONS[0])
    require(all(r["api_commit"] == primary_api for r in after if r["service"] == SERVICES[1]),
            "public document misidentifies the API release")
    for path in ("/", "/about", "/blog", "/docs", "/static/homepage/application.css",
                 "/static/homepage/components.css", "/health", "/ready", "/status.json",
                 "/v1/models", "/static/charter.css"):
        command("curl", "--fail", "--silent", "--show-error", "--max-time", "20",
                "--output", "/dev/null", "https://trustedrouter.com" + path)
    command("bash", "scripts/deploy/public_surface_edge.sh", "check")
    command("bash", "scripts/deploy/verify_gcp_complete.sh", timeout=900)
    return after


def request_full_rollout() -> None:
    require(os.environ.get("TR_WEBSITE_ONLY") != "true",
            "Website-only refused: runtime inputs differ; no API rollout was requested")
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write("full_rollout=true\n")


def publish() -> None:
    release = validated_release()
    before = inventory()
    if not eligible(before, release):
        print("docs: runtime inputs differ; use the full control-plane rollout")
        request_full_rollout()
        return
    # Admission is exclusive against gateways AND control planes, not a borrowed fence.
    require(not os.environ.get("TR_DEPLOY_MUTEX_OPERATION"), "docs lane cannot borrow a lease")
    env = dict(os.environ, TR_DEPLOY_MUTEX_CLOUD="gcp", TR_DEPLOY_COMPONENT="public-surface")
    fence = command("bash", "scripts/deploy/deploy_mutex.sh", "acquire", env=env, timeout=14700)
    allowed = {"TR_DEPLOY_MUTEX_OPERATION", "TR_DEPLOY_MUTEX_GENERATION",
               "TR_DEPLOY_MUTEX_CREATED_AT", "TR_DEPLOY_MUTEX_CLOUD"}
    assignments = dict(line.split("=", 1) for line in fence.splitlines())
    require(set(assignments) == allowed and assignments["TR_DEPLOY_MUTEX_CLOUD"] == "gcp",
            "invalid docs admission fence")
    env.update(assignments)
    outcome = "success"  # Until mutation starts, a read-only refusal can close normally.
    try:
        release = validated_release()
        before = inventory()
        if not eligible(before, release):
            request_full_rollout()
            return
        command("bash", "scripts/deploy/public_surface_edge.sh", "check", env=env)
        MANIFEST.write_text(json.dumps({"release": asdict(release), "before": before}) + "\n")
        env["TR_PUBLIC_DOCS_MANIFEST"] = str(MANIFEST)
        outcome = "failure"
        # Stream only the existing sanitized deployment log, never revision JSON.
        result = subprocess.run(["bash", "scripts/deploy/public_surface.sh", "routed"],  # noqa: S603,S607
                                env=env, timeout=1800)
        require(result.returncode == 0, "public docs rollout failed")
        command("bash", "scripts/deploy/deploy_mutex.sh", "assert", env=env)
        after = verify(before, release)
        command("bash", "scripts/deploy/deploy_mutex.sh", "assert", env=env)
        receipt = {"schema_version": 1, "release": asdict(release), "before": before, "after": after,
                   "controller_commit": release.commit, "controller_run": int(os.environ["GITHUB_RUN_ID"]),
                   "controller_attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]), "surface": "public-docs"}
        Path("release.json").write_text(json.dumps(receipt, sort_keys=True) + "\n")
        outcome = "success"
    finally:
        command("bash", "scripts/deploy/deploy_mutex.sh", "release",
                env=dict(env, TR_DEPLOY_OUTCOME=outcome))
    with Path(os.environ["GITHUB_OUTPUT"]).open("a") as output:
        output.write("full_rollout=false\ndelivered=true\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("scope", "publish", "candidate", "guard-source"))
    parser.add_argument("--source", default="")
    parser.add_argument("--target", default="")
    args = parser.parse_args()
    if args.command == "scope":
        print(str(proposed_docs_only()).lower())
    elif args.command == "candidate":
        candidate()
    elif args.command == "guard-source":
        guard_source(args.source, args.target)
    else:
        publish()


if __name__ == "__main__":
    main()
