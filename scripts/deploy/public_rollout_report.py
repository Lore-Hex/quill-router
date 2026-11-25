"""Observe full public rollouts. Never authorize a skip or change cloud state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from scripts.deploy.public_docs import PROJECT, REGIONS, SERVICES, command, commit
from scripts.deploy.release_train import IMAGE, Refused, require

SERVICE = SERVICES[1]
FIELDS = ("image", "website_source", "api_source", "configuration")


def signature(service: dict[str, Any], revision: dict[str, Any]) -> dict[str, str]:
    """Fingerprint the serving configuration; never persist raw environment values."""
    name = revision["metadata"]["name"]
    require(service["metadata"]["name"] == SERVICE and name.startswith(SERVICE + "-"),
            "unexpected public service or revision")
    traffic = service["status"]["traffic"]
    require(len(traffic) == 1 and traffic[0].get("revisionName") == name
            and traffic[0].get("percent") == 100 and not traffic[0].get("tag"), "ambiguous traffic")
    for resource in (service, revision):
        ready = [c["status"] for c in resource["status"]["conditions"] if c["type"] == "Ready"]
        require(ready == ["True"], "not ready")
    annotations = service["metadata"]["annotations"]
    require(annotations["run.googleapis.com/ingress"] == "internal-and-cloud-load-balancing",
            "not routed")
    spec = revision["spec"]
    container, = spec["containers"]
    image = container["image"]
    require(re.fullmatch(re.escape(IMAGE) + r"[0-9a-f]{64}", image) is not None, "mutable image")
    route, = service["spec"]["traffic"]
    require(route.get("percent") == 100 and not route.get("tag") and
            (route.get("revisionName") == name or
             (route.get("latestRevision") is True and service["status"].get("latestReadyRevisionName") == name)),
            "desired traffic differs from observed")
    sources = {}
    for field in ("TR_RELEASE", "TR_PUBLIC_SOURCE_COMMIT"):
        value, = [e["value"] for e in container["env"] if e["name"] == field]
        sources[field] = commit(value)

    def config_spec(raw: dict[str, Any]) -> dict[str, Any]:
        item, = raw["containers"]
        other_env = [e for e in item["env"] if e not in container["env"] or e["name"] not in sources]
        return {**raw, "containers": [{**item, "env": other_env,
                                       "image": "serving" if item["image"] == image else item["image"]}]}
    # Ignore only identifiers assigned to each new revision or operation. Keep
    # unknown annotations and labels: false differences are safer than false matches.
    def config_meta(metadata: dict[str, Any]) -> dict[str, Any]:
        generated = {"run.googleapis.com/operation-id", "serving.knative.dev/configurationGeneration",
                     "serving.knative.dev/route", "serving.knative.dev/serviceUid",
                     "serving.knative.dev/configurationUid"}
        return {key: {k: v for k, v in metadata.get(key, {}).items() if k not in generated}
                for key in ("annotations", "labels")}

    configuration = [config_spec(spec), config_meta(revision["metadata"]), config_meta(service["metadata"]),
                     {**route, **({"revisionName": "serving"} if route.get("revisionName") == name else {})},
                     {k: v for k, v in service["spec"].items() if k not in {"traffic", "template"}},
                     config_meta(service["spec"]["template"]["metadata"]),
                     config_spec(service["spec"]["template"]["spec"])]
    digest = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
    return {"image": image, "website_source": sources["TR_PUBLIC_SOURCE_COMMIT"],
            "api_source": sources["TR_RELEASE"], "configuration": digest}


def observe(region: str) -> dict[str, str]:
    def describe(kind: str, name: str) -> Any:
        return json.loads(command("gcloud", "--project", PROJECT, "run", kind, "describe", name,
                                  "--region", region, "--format=json", timeout=15))
    try:
        service = describe("services", SERVICE)
        traffic, = service["status"]["traffic"]
        name = traffic["revisionName"]
        require(isinstance(name, str) and name.startswith(SERVICE + "-"), "bad revision")
        return signature(service, describe("revisions", name))
    except (Refused, KeyError, ValueError, TypeError, OSError, subprocess.TimeoutExpired):
        return {}


def compare(before: dict[str, str], after: dict[str, str], image: str, source: str) -> str:
    if (not all(before.get(k) and after.get(k) for k in FIELDS) or
            after["image"] != image or after["website_source"] != source or after["api_source"] != source):
        return "unknown"
    if changed := [k for k in FIELDS if before[k] != after[k]]:
        return "changed: " + ", ".join(changed)
    return "observed match"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("before", "after"))
    args = parser.parse_args()
    baseline = Path(os.environ["RUNNER_TEMP"]) / "public-rollout-before.json"
    with ThreadPoolExecutor(max_workers=4) as pool:
        fleet = dict(zip(REGIONS, pool.map(observe, REGIONS), strict=True))
    if args.phase == "before":
        baseline.write_text(json.dumps(fleet))
        return
    before = json.loads(baseline.read_text()) if baseline.is_file() else {}
    results = {region: compare(before.get(region, {}), fleet[region], os.environ["EXPECTED_IMAGE"],
                               os.environ["EXPECTED_COMMIT"]) for region in REGIONS}
    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as out:
        print("### Website redeploy comparison (report only)\n", file=out)
        for region, result in results.items():
            print(f"* {region}: {result}", file=out)
        print("\nFleet match:", "yes" if set(results.values()) == {"observed match"} else "not proven",
              "\n\nThis compares the immutable image, release sources, serving traffic and Cloud Run "
              "configuration. Secret bindings are compared, not the values behind mutable versions. "
              "No rollout or smoke test is skipped.", file=out)


if __name__ == "__main__":
    main()
