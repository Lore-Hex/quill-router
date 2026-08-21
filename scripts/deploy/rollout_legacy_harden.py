#!/usr/bin/env python3
"""Journaled forward hardening for the retained legacy Cloud Run monolith."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,61}\Z")
PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
REGION_RE = re.compile(r"[a-z]+-[a-z0-9]+[0-9]\Z")
SA_RE = re.compile(
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9]"
    r"[.]iam[.]gserviceaccount[.]com\Z"
)
COMPUTE_SA_RE = re.compile(r"[1-9][0-9]{5,19}-compute@developer[.]gserviceaccount[.]com\Z")
IMAGE_RE = re.compile(r"[^\s,@]+@sha256:[0-9a-f]{64}\Z")
SECRET_RE = re.compile(r"[A-Za-z0-9_-]{1,255}\Z")
INGRESS = "internal-and-cloud-load-balancing"
JOURNAL_STATES = {
    "captured",
    "normalize_intent",
    "normalized",
    "deploy_intent",
    "deployed",
    "ramp10_intent",
    "ramp10",
    "ramp50_intent",
    "ramp50",
    "ramp100_intent",
    "ramp100",
    "rollback_intent",
    "rolled_back",
}
JOURNAL_FIELDS = {
    "schema_version",
    "kind",
    "project_id",
    "service",
    "runtime_service_account",
    "operation_id",
    "revision_suffix",
    "created_at",
    "updated_at",
    "secret_refs",
    "regions",
}
JOURNAL_REGION_FIELDS = {
    "region",
    "state",
    "baseline_revision",
    "baseline_was_latest",
    "baseline_revision_sha256",
    "image",
    "iam_sha256",
    "expected_candidate_revision_sha256",
    "candidate_revision",
    "rollback_percent",
}
ARTIFACT_FIELDS = {
    "schema_version",
    "kind",
    "project_id",
    "service",
    "runtime_service_account",
    "operation_id",
    "revision_suffix",
    "regions",
    "secret_refs",
    "journal_sha256",
    "created_at",
}
ARTIFACT_REGION_FIELDS = {
    "region",
    "serving_revision",
    "service_sha256",
    "revision_sha256",
    "iam_sha256",
}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_timestamp(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("legacy hardening timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("legacy hardening timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise ValueError("legacy hardening timestamp must include a timezone")


def _atomic(path: Path, value: Any, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if exclusive and path.exists():
        raise ValueError(f"refusing to overwrite recovery artifact: {path}")
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        if exclusive:
            os.link(temporary, path)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


class Cloud:
    def __init__(self, project: str) -> None:
        self.project = project
        executable = shutil.which("gcloud")
        if executable is None:
            raise ValueError("gcloud is required for legacy hardening")
        self.executable = executable

    def run(self, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(  # noqa: S603
            [self.executable, "--project", self.project, *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        if check and result.returncode:
            raise ValueError("read-only gcloud inspection failed")
        return result

    def json(self, *arguments: str) -> Any:
        result = self.run(*arguments, "--format=json")
        return json.loads(result.stdout)

    def optional_json(self, *arguments: str) -> Any | None:
        result = self.run(*arguments, "--format=json", check=False)
        if result.returncode:
            return None
        return json.loads(result.stdout)


def _basename(value: Any) -> str:
    return str(value or "").rstrip("/").split("/")[-1]


def _container(value: dict[str, Any]) -> dict[str, Any]:
    template = ((value.get("spec") or {}).get("template") or {}).get("spec") or {}
    containers = template.get("containers") or []
    if len(containers) != 1 or template.get("initContainers") or template.get("volumes"):
        raise ValueError("legacy hardening requires exactly one container and no volumes")
    container = containers[0]
    if container.get("volumeMounts"):
        raise ValueError("legacy hardening does not support secret or data volumes")
    return container


def _revision_container(value: dict[str, Any]) -> dict[str, Any]:
    spec = value.get("spec") or {}
    containers = spec.get("containers") or []
    if len(containers) != 1 or spec.get("initContainers") or spec.get("volumes"):
        raise ValueError("legacy hardening requires exactly one revision container")
    container = containers[0]
    if container.get("volumeMounts"):
        raise ValueError("legacy hardening does not support revision volume mounts")
    return container


def _service_semantic(service: dict[str, Any]) -> dict[str, Any]:
    metadata = service.get("metadata") or {}
    spec = service.get("spec") or {}
    template = spec.get("template") or {}
    template_metadata = template.get("metadata") or {}
    template_spec = template.get("spec") or {}
    annotations = metadata.get("annotations") or {}
    template_annotations = template_metadata.get("annotations") or {}
    selected_annotations = {
        key: value
        for key, value in annotations.items()
        if key
        in {
            "run.googleapis.com/ingress",
            "run.googleapis.com/ingress-status",
            "run.googleapis.com/default-url-disabled",
            "run.googleapis.com/maxScale",
            "run.googleapis.com/minScale",
        }
    }
    selected_template_annotations = {
        key: value
        for key, value in template_annotations.items()
        if key
        in {
            "autoscaling.knative.dev/minScale",
            "autoscaling.knative.dev/maxScale",
            "run.googleapis.com/vpc-access-egress",
            "run.googleapis.com/network-interfaces",
            "run.googleapis.com/startup-cpu-boost",
        }
    }
    containers = []
    for container in template_spec.get("containers") or []:
        copied = {
            key: container[key]
            for key in (
                "image",
                "ports",
                "resources",
                "startupProbe",
                "command",
                "args",
                "volumeMounts",
            )
            if key in container
        }
        copied["env"] = sorted(
            container.get("env") or [], key=lambda item: str(item.get("name", ""))
        )
        containers.append(copied)
    return {
        "metadata": {"annotations": selected_annotations},
        "spec": {
            "scaling": spec.get("scaling") or {},
            "template": {
                "metadata": {
                    "annotations": selected_template_annotations,
                    "name": template_metadata.get("name"),
                },
                "spec": {
                    "containerConcurrency": template_spec.get("containerConcurrency"),
                    "serviceAccountName": template_spec.get("serviceAccountName"),
                    "timeoutSeconds": template_spec.get("timeoutSeconds"),
                    "volumes": template_spec.get("volumes") or [],
                    "initContainers": template_spec.get("initContainers") or [],
                    "containers": containers,
                },
            },
        },
    }


def _secret_resource(value: Any, project: str) -> str:
    if not isinstance(value, str):
        raise ValueError("legacy secret resource is not a string")
    if SECRET_RE.fullmatch(value):
        return value
    match = re.fullmatch(r"projects/([^/]+)/secrets/([^/]+)", value)
    if match is None or match.group(1) != project or not SECRET_RE.fullmatch(match.group(2)):
        raise ValueError("legacy secret resource is not local and canonical")
    return match.group(2)


def _secret_refs(container: dict[str, Any], project: str) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in container.get("env") or []:
        reference = (item.get("valueFrom") or {}).get("secretKeyRef") or {}
        if not reference:
            continue
        name = item.get("name")
        resource = _secret_resource(reference.get("name"), project)
        version = str(reference.get("key") or "")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name)
            or name in seen
            or not (version == "latest" or re.fullmatch(r"[1-9][0-9]*", version))
        ):
            raise ValueError("legacy secret reference is malformed or duplicated")
        seen.add(name)
        refs.append({"name": name, "resource": resource, "version": version})
    return sorted(refs, key=lambda item: item["name"])


def _revision_semantic(revision: dict[str, Any]) -> dict[str, Any]:
    metadata = revision.get("metadata") or {}
    spec = copy.deepcopy(revision.get("spec") or {})
    return {
        "annotations": {
            key: value
            for key, value in sorted((metadata.get("annotations") or {}).items())
            if not key.startswith("client.knative.dev/")
            and key
            not in {
                "run.googleapis.com/client-name",
                "run.googleapis.com/client-version",
                "run.googleapis.com/operation-id",
                "serving.knative.dev/creator",
                "serving.knative.dev/lastModifier",
            }
        },
        "spec": spec,
    }


def _replace_secret_versions(value: dict[str, Any], pins: list[dict[str, str]]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    by_name = {item["name"]: item for item in pins}
    containers = (result.get("spec") or {}).get("containers") or []
    if len(containers) != 1:
        raise ValueError("legacy revision semantic has inexact containers")
    for item in containers[0].get("env") or []:
        reference = (item.get("valueFrom") or {}).get("secretKeyRef") or {}
        if reference:
            expected = by_name.get(item.get("name"))
            if expected is None or _basename(reference.get("name")) != expected["resource"]:
                raise ValueError("legacy revision secret resources differ across regions")
            reference["key"] = expected["version"]
    return result


def _traffic(service: dict[str, Any]) -> tuple[str, bool]:
    desired = (service.get("spec") or {}).get("traffic") or []
    observed = (service.get("status") or {}).get("traffic") or []
    if len(desired) != 1 or len(observed) != 1:
        raise ValueError("legacy traffic must have one 100-percent target")
    target, live = desired[0], observed[0]
    if target.get("tag") is not None or live.get("tag") is not None:
        raise ValueError("legacy traffic tags must be removed before hardening")
    if target.get("percent") != 100 or live.get("percent") != 100:
        raise ValueError("legacy traffic must serve exactly 100 percent")
    revision = _basename(live.get("revisionName"))
    if not NAME_RE.fullmatch(revision):
        raise ValueError("legacy serving revision is invalid")
    desired_revision = _basename(target.get("revisionName"))
    latest = target.get("latestRevision") is True
    if not latest and desired_revision != revision:
        raise ValueError("legacy desired and observed traffic differ")
    return revision, latest


def _ready(service: dict[str, Any], revision: str) -> None:
    metadata = service.get("metadata") or {}
    status = service.get("status") or {}
    if status.get("observedGeneration") != metadata.get("generation"):
        raise ValueError("legacy service generation is not observed")
    if status.get("latestReadyRevisionName") != revision:
        raise ValueError("legacy serving revision is not latest Ready")
    ready = [
        item
        for item in status.get("conditions") or []
        if item.get("type") == "Ready" and item.get("status") == "True"
    ]
    if len(ready) != 1:
        raise ValueError("legacy service is not exactly Ready")


def _ready_revision(value: dict[str, Any], expected_name: str) -> None:
    if _basename((value.get("metadata") or {}).get("name")) != expected_name:
        raise ValueError("legacy revision identity differs")
    ready = [
        item
        for item in (value.get("status") or {}).get("conditions") or []
        if item.get("type") == "Ready" and item.get("status") == "True"
    ]
    if len(ready) != 1:
        raise ValueError("legacy revision is not exactly Ready")


def _iam(cloud: Cloud, service: str, region: str) -> str:
    policy = cloud.json("run", "services", "get-iam-policy", service, f"--region={region}")
    matches = []
    for binding in policy.get("bindings") or []:
        members = binding.get("members") or []
        if "allAuthenticatedUsers" in members:
            raise ValueError("legacy service IAM contains allAuthenticatedUsers")
        if "allUsers" in members:
            matches.append(binding)
    if (
        len(matches) != 1
        or matches[0].get("role") != "roles/run.invoker"
        or matches[0].get("condition")
        or (matches[0].get("members") or []).count("allUsers") != 1
    ):
        raise ValueError("legacy service requires one unconditional public invoker binding")
    bindings = [
        {
            "role": binding.get("role"),
            "members": sorted(binding.get("members") or []),
            "condition": binding.get("condition") or None,
        }
        for binding in policy.get("bindings") or []
    ]
    bindings.sort(key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")))
    return _sha({"bindings": bindings})


def _verify_secret(cloud: Cloud, resource: str, version: str, runtime_member: str) -> None:
    metadata = cloud.json("secrets", "versions", "describe", version, f"--secret={resource}")
    if metadata.get("state") != "ENABLED" or _basename(metadata.get("name")) != version:
        raise ValueError("legacy pinned secret version is not enabled")
    policy = cloud.json("secrets", "get-iam-policy", resource)
    public = {"allUsers", "allAuthenticatedUsers"}
    access = []
    for binding in policy.get("bindings") or []:
        members = set(binding.get("members") or [])
        if members & public:
            raise ValueError("legacy secret policy contains a public principal")
        if runtime_member in members:
            if (binding.get("members") or []).count(runtime_member) != 1:
                raise ValueError("legacy runtime secret access is duplicated")
            access.append(binding)
    if (
        len(access) != 1
        or access[0].get("role") != "roles/secretmanager.secretAccessor"
        or access[0].get("condition")
    ):
        raise ValueError("legacy runtime secret access is not exact")


def _resolve_pins(cloud: Cloud, refs_by_region: list[list[dict[str, str]]]) -> list[dict[str, str]]:
    shapes = [[(item["name"], item["resource"]) for item in refs] for refs in refs_by_region]
    if not shapes or any(shape != shapes[0] for shape in shapes[1:]):
        raise ValueError("legacy secret resource mappings differ across regions")
    pins: list[dict[str, str]] = []
    for index, item in enumerate(refs_by_region[0]):
        resolved_versions: set[str] = set()
        for regional_refs in refs_by_region:
            version = regional_refs[index]["version"]
            if version == "latest":
                latest = cloud.json(
                    "secrets",
                    "versions",
                    "describe",
                    "latest",
                    f"--secret={item['resource']}",
                )
                if latest.get("state") != "ENABLED":
                    raise ValueError("legacy latest secret version is not enabled")
                version = _basename(latest.get("name"))
            if not re.fullmatch(r"[1-9][0-9]*", version):
                raise ValueError("legacy resolved secret version is not numeric")
            resolved_versions.add(version)
        if len(resolved_versions) != 1:
            raise ValueError("legacy mounted secret versions differ across regions")
        pins.append({**item, "version": resolved_versions.pop()})
    return pins


def _describe_service(cloud: Cloud, service: str, region: str) -> dict[str, Any]:
    return cloud.json("run", "services", "describe", service, f"--region={region}")


def _describe_revision(cloud: Cloud, revision: str, region: str) -> dict[str, Any]:
    return cloud.json("run", "revisions", "describe", revision, f"--region={region}")


def _normalized_traffic(items: list[dict[str, Any]]) -> dict[str, int]:
    result: dict[str, int] = {}
    seen: set[str] = set()
    for item in items:
        if item.get("latestRevision") is True or item.get("tag") is not None:
            raise ValueError("legacy hardening traffic must stay named and untagged")
        revision = _basename(item.get("revisionName"))
        percent = item.get("percent")
        if (
            not NAME_RE.fullmatch(revision)
            or isinstance(percent, bool)
            or not isinstance(percent, int)
        ):
            raise ValueError("legacy hardening traffic target is malformed")
        if not 0 <= percent <= 100:
            raise ValueError("legacy hardening traffic percent is invalid")
        if percent == 0 or revision in seen:
            raise ValueError("legacy traffic contains a duplicate or zero-percent target")
        seen.add(revision)
        result[revision] = percent
    if sum(result.values()) != 100:
        raise ValueError("legacy hardening traffic does not total 100 percent")
    return result


def _allocation(
    cloud: Cloud, service_name: str, region: str
) -> tuple[dict[str, Any], dict[str, int]]:
    service = _describe_service(cloud, service_name, region)
    desired = _normalized_traffic((service.get("spec") or {}).get("traffic") or [])
    observed = _normalized_traffic((service.get("status") or {}).get("traffic") or [])
    if desired != observed:
        raise ValueError("legacy desired and observed traffic have not converged")
    return service, desired


def _verify_allocation(
    cloud: Cloud, service_name: str, region: str, baseline: str, candidate: str, percent: int
) -> None:
    _, actual = _allocation(cloud, service_name, region)
    expected: dict[str, int] = {}
    if percent:
        expected[candidate] = percent
    if percent < 100:
        expected[baseline] = 100 - percent
    if actual != expected:
        raise ValueError("legacy traffic ramp postcondition differs")


def _set_state(path: Path, journal: dict[str, Any], region: str, state: str) -> None:
    if state not in JOURNAL_STATES:
        raise ValueError("legacy hardening journal transition is invalid")
    entry = next(item for item in journal["regions"] if item["region"] == region)
    entry["state"] = state
    journal["updated_at"] = datetime.now(UTC).isoformat()
    _atomic(path, journal)


def _validate_secret_refs(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        raise ValueError("legacy hardening secret refs are not a list")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "resource", "version"}:
            raise ValueError("legacy hardening secret ref fields differ")
        name = item["name"]
        resource = item["resource"]
        version = item["version"]
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", name)
            or name in seen
            or not isinstance(resource, str)
            or not SECRET_RE.fullmatch(resource)
            or not isinstance(version, str)
            or not re.fullmatch(r"[1-9][0-9]*", version)
        ):
            raise ValueError("legacy hardening secret ref is invalid or duplicated")
        seen.add(name)
        result.append(item)
    if result != sorted(result, key=lambda item: item["name"]):
        raise ValueError("legacy hardening secret refs are not canonical")
    return result


def _validate_journal(
    args: argparse.Namespace,
    value: Any,
    regions: list[str],
    *,
    path: Path,
) -> dict[str, Any]:
    if path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("legacy hardening state must be mode 0600")
    if not isinstance(value, dict) or set(value) != JOURNAL_FIELDS:
        raise ValueError("legacy hardening state fields differ")
    if (
        value["schema_version"] != 1
        or value["kind"] != "trusted-router-legacy-hardening-state"
        or value["project_id"] != args.project
        or value["service"] != args.service
        or value["runtime_service_account"] != args.runtime_service_account
        or value["operation_id"] != args.operation_id
    ):
        raise ValueError("legacy hardening state identity differs")
    _validate_timestamp(value["created_at"])
    _validate_timestamp(value["updated_at"])
    suffix = value["revision_suffix"]
    if (
        not isinstance(suffix, str)
        or not NAME_RE.fullmatch(suffix)
        or len(f"{args.service}-{suffix}") > 63
    ):
        raise ValueError("legacy hardening state suffix is invalid")
    _validate_secret_refs(value["secret_refs"])
    entries = value["regions"]
    if (
        not isinstance(entries, list)
        or any(not isinstance(item, dict) for item in entries)
        or [item.get("region") for item in entries] != regions
    ):
        raise ValueError("legacy hardening state region inventory differs")
    for item in entries:
        if not isinstance(item, dict) or set(item) != JOURNAL_REGION_FIELDS:
            raise ValueError("legacy hardening regional state fields differ")
        if (
            item["state"] not in JOURNAL_STATES
            or not isinstance(item["baseline_was_latest"], bool)
            or not NAME_RE.fullmatch(str(item["baseline_revision"]))
            or item["candidate_revision"] != f"{args.service}-{suffix}"
            or not IMAGE_RE.fullmatch(str(item["image"]))
            or any(
                not re.fullmatch(r"[0-9a-f]{64}", str(item[field]))
                for field in (
                    "baseline_revision_sha256",
                    "iam_sha256",
                    "expected_candidate_revision_sha256",
                )
            )
            or (
                item["rollback_percent"] is not None
                and item["rollback_percent"] not in {0, 10, 50, 100}
            )
            or (
                item["state"] in {"rollback_intent", "rolled_back"}
                and item["rollback_percent"] is None
            )
            or (
                item["state"] not in {"rollback_intent", "rolled_back"}
                and item["rollback_percent"] is not None
            )
        ):
            raise ValueError("legacy hardening regional state is invalid")
    if len({item["image"] for item in entries}) != 1:
        raise ValueError("legacy hardening state image cohort differs")
    return value


def _validate_inputs(args: argparse.Namespace) -> list[str]:
    if not PROJECT_RE.fullmatch(args.project) or not NAME_RE.fullmatch(args.service):
        raise ValueError("legacy hardening project/service is invalid")
    if not (
        (
            SA_RE.fullmatch(args.runtime_service_account)
            and args.runtime_service_account.endswith(f"@{args.project}.iam.gserviceaccount.com")
        )
        or COMPUTE_SA_RE.fullmatch(args.runtime_service_account)
    ):
        raise ValueError("legacy runtime service account is invalid")
    if args.artifact is not None and (
        not isinstance(args.operation_id, str)
        or not re.fullmatch(r"[A-Za-z0-9._:-]{8,160}", args.operation_id)
    ):
        raise ValueError("legacy hardening operation ID is invalid")
    regions = args.regions.split(",")
    if (
        not regions
        or len(regions) != len(set(regions))
        or any(not REGION_RE.fullmatch(region) for region in regions)
    ):
        raise ValueError("legacy hardening region inventory is invalid")
    return regions


def _capture_plan(
    args: argparse.Namespace, cloud: Cloud, regions: list[str], suffix: str
) -> dict[str, Any]:
    captured: list[dict[str, Any]] = []
    refs_by_region: list[list[dict[str, str]]] = []
    for region in regions:
        candidate = f"{args.service}-{suffix}"
        if (
            cloud.optional_json("run", "revisions", "describe", candidate, f"--region={region}")
            is not None
        ):
            raise ValueError("legacy hardening candidate already exists without this journal")
        service = _describe_service(cloud, args.service, region)
        baseline, latest = _traffic(service)
        _ready(service, baseline)
        revision = _describe_revision(cloud, baseline, region)
        _ready_revision(revision, baseline)
        container = _revision_container(revision)
        if (revision.get("spec") or {}).get("serviceAccountName") != args.runtime_service_account:
            raise ValueError("legacy serving revision runtime identity differs")
        image = container.get("image")
        if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
            raise ValueError("legacy serving image must be immutable")
        refs = _secret_refs(container, args.project)
        refs_by_region.append(refs)
        captured.append(
            {
                "region": region,
                "state": "captured",
                "baseline_revision": baseline,
                "baseline_was_latest": latest,
                "baseline_revision_sha256": _sha(_revision_semantic(revision)),
                "image": image,
                "iam_sha256": _iam(cloud, args.service, region),
                "rollback_percent": None,
            }
        )
    pins = _resolve_pins(cloud, refs_by_region)
    if len({entry["image"] for entry in captured}) != 1:
        raise ValueError("legacy serving image differs across regions")
    runtime_member = f"serviceAccount:{args.runtime_service_account}"
    for pin in pins:
        _verify_secret(cloud, pin["resource"], pin["version"], runtime_member)
    for entry in captured:
        revision = _describe_revision(cloud, entry["baseline_revision"], entry["region"])
        expected = _replace_secret_versions(_revision_semantic(revision), pins)
        entry["expected_candidate_revision_sha256"] = _sha(expected)
        entry["candidate_revision"] = f"{args.service}-{suffix}"
    return {
        "schema_version": 1,
        "kind": "trusted-router-legacy-hardening-state",
        "project_id": args.project,
        "service": args.service,
        "runtime_service_account": args.runtime_service_account,
        "operation_id": args.operation_id,
        "revision_suffix": suffix,
        "created_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "secret_refs": pins,
        "regions": captured,
    }


def _verify_candidate(
    args: argparse.Namespace, cloud: Cloud, journal: dict[str, Any], entry: dict[str, Any]
) -> None:
    region = entry["region"]
    service = _describe_service(cloud, args.service, region)
    candidate = entry["candidate_revision"]
    _ready(service, candidate)
    annotations = (service.get("metadata") or {}).get("annotations") or {}
    if (
        annotations.get("run.googleapis.com/ingress") != INGRESS
        or annotations.get("run.googleapis.com/ingress-status") != INGRESS
    ):
        raise ValueError("legacy hardening ingress postcondition differs")
    service_template = ((service.get("spec") or {}).get("template") or {}).get("spec") or {}
    if service_template.get("serviceAccountName") != args.runtime_service_account:
        raise ValueError("legacy service template runtime identity differs")
    if _container(service).get("image") != entry["image"]:
        raise ValueError("legacy service template image differs")
    revision = _describe_revision(cloud, candidate, region)
    _ready_revision(revision, candidate)
    revision_spec = revision.get("spec") or {}
    if revision_spec.get("serviceAccountName") != args.runtime_service_account:
        raise ValueError("legacy candidate runtime identity differs")
    if _sha(_revision_semantic(revision)) != entry["expected_candidate_revision_sha256"]:
        raise ValueError("legacy candidate revision differs from journaled contract")
    if _revision_container(revision).get("image") != entry["image"]:
        raise ValueError("legacy candidate image differs")
    runtime_member = f"serviceAccount:{args.runtime_service_account}"
    for pin in journal["secret_refs"]:
        _verify_secret(cloud, pin["resource"], pin["version"], runtime_member)
    if _iam(cloud, args.service, region) != entry["iam_sha256"]:
        raise ValueError("legacy service invoker IAM drifted")


def _current_candidate_percent(
    args: argparse.Namespace,
    cloud: Cloud,
    entry: dict[str, Any],
) -> int:
    _, allocation = _allocation(cloud, args.service, entry["region"])
    baseline = entry["baseline_revision"]
    candidate = entry["candidate_revision"]
    if not set(allocation).issubset({baseline, candidate}):
        raise ValueError("legacy traffic contains an unowned revision")
    percent = allocation.get(candidate, 0)
    expected_baseline = 0 if percent == 100 else 100 - percent
    if allocation.get(baseline, 0) != expected_baseline:
        raise ValueError("legacy traffic allocation is not an owned cohort")
    return percent


def _normalize_entry(
    args: argparse.Namespace,
    cloud: Cloud,
    journal: dict[str, Any],
    path: Path,
    entry: dict[str, Any],
) -> None:
    if entry["state"] not in {"captured", "normalize_intent"}:
        return
    _verify_baseline(args, cloud, journal, entry)
    service = _describe_service(cloud, args.service, entry["region"])
    serving, latest = _traffic(service)
    if serving != entry["baseline_revision"]:
        raise ValueError("legacy baseline traffic drifted before normalization")
    if entry["state"] == "captured":
        if not latest:
            _verify_allocation(
                cloud,
                args.service,
                entry["region"],
                entry["baseline_revision"],
                entry["candidate_revision"],
                0,
            )
            _set_state(path, journal, entry["region"], "normalized")
            return
        _set_state(path, journal, entry["region"], "normalize_intent")
    if latest:
        cloud.run(
            "run",
            "services",
            "update-traffic",
            args.service,
            f"--region={entry['region']}",
            f"--to-revisions={entry['baseline_revision']}=100",
            "--clear-tags",
            "--quiet",
            check=False,
        )
    _verify_allocation(
        cloud,
        args.service,
        entry["region"],
        entry["baseline_revision"],
        entry["candidate_revision"],
        0,
    )
    _set_state(path, journal, entry["region"], "normalized")


def _candidate_exists(cloud: Cloud, entry: dict[str, Any]) -> bool:
    return (
        cloud.optional_json(
            "run",
            "revisions",
            "describe",
            entry["candidate_revision"],
            f"--region={entry['region']}",
        )
        is not None
    )


def _deploy_entry(
    args: argparse.Namespace,
    cloud: Cloud,
    journal: dict[str, Any],
    path: Path,
    entry: dict[str, Any],
) -> None:
    _verify_baseline(args, cloud, journal, entry)
    if entry["state"] == "rolled_back":
        entry["rollback_percent"] = None
        if entry["baseline_was_latest"]:
            service = _describe_service(cloud, args.service, entry["region"])
            _, latest = _traffic(service)
            if latest:
                _set_state(path, journal, entry["region"], "normalize_intent")
                _normalize_entry(args, cloud, journal, path, entry)
            else:
                _set_state(path, journal, entry["region"], "normalized")
        else:
            _set_state(path, journal, entry["region"], "normalized")
    _normalize_entry(args, cloud, journal, path, entry)
    if entry["state"] not in {"normalized", "deploy_intent", "deployed"}:
        return
    if entry["state"] == "deployed":
        _verify_candidate(args, cloud, journal, entry)
        _verify_allocation(
            cloud,
            args.service,
            entry["region"],
            entry["baseline_revision"],
            entry["candidate_revision"],
            0,
        )
        return
    if entry["state"] == "normalized":
        _set_state(path, journal, entry["region"], "deploy_intent")
    if not _candidate_exists(cloud, entry):
        secrets = ",".join(
            f"{item['name']}={item['resource']}:{item['version']}"
            for item in journal["secret_refs"]
        )
        command = [
            "run",
            "services",
            "update",
            args.service,
            f"--region={entry['region']}",
            f"--revision-suffix={journal['revision_suffix']}",
            f"--image={entry['image']}",
            f"--service-account={args.runtime_service_account}",
            "--no-traffic",
            f"--ingress={INGRESS}",
            "--quiet",
        ]
        if secrets:
            command.append(f"--set-secrets={secrets}")
        cloud.run(*command, check=False)
    _verify_candidate(args, cloud, journal, entry)
    _verify_allocation(
        cloud,
        args.service,
        entry["region"],
        entry["baseline_revision"],
        entry["candidate_revision"],
        0,
    )
    _set_state(path, journal, entry["region"], "deployed")


def _allowed_percent(entry: dict[str, Any]) -> set[int]:
    state = entry["state"]
    if state in {
        "captured",
        "normalize_intent",
        "normalized",
        "deploy_intent",
        "deployed",
        "rolled_back",
    }:
        return {0}
    if state == "ramp10_intent":
        return {0, 10}
    if state == "ramp10":
        return {10}
    if state == "ramp50_intent":
        return {10, 50}
    if state == "ramp50":
        return {50}
    if state == "ramp100_intent":
        return {50, 100}
    if state == "ramp100":
        return {100}
    if state == "rollback_intent":
        return {0, int(entry["rollback_percent"] or 0)}
    raise ValueError("legacy hardening journal state has no traffic contract")


def _verify_baseline(
    args: argparse.Namespace,
    cloud: Cloud,
    journal: dict[str, Any],
    entry: dict[str, Any],
) -> None:
    revision = _describe_revision(cloud, entry["baseline_revision"], entry["region"])
    _ready_revision(revision, entry["baseline_revision"])
    if _sha(_revision_semantic(revision)) != entry["baseline_revision_sha256"]:
        raise ValueError("legacy baseline revision drifted")
    if _iam(cloud, args.service, entry["region"]) != entry["iam_sha256"]:
        raise ValueError("legacy invoker IAM drifted")
    runtime_member = f"serviceAccount:{args.runtime_service_account}"
    for pin in journal["secret_refs"]:
        _verify_secret(cloud, pin["resource"], pin["version"], runtime_member)


def _rollback(
    args: argparse.Namespace,
    cloud: Cloud,
    journal: dict[str, Any],
    path: Path,
) -> None:
    percents: dict[str, int] = {}
    for entry in journal["regions"]:
        _verify_baseline(args, cloud, journal, entry)
        try:
            percent = _current_candidate_percent(args, cloud, entry)
        except ValueError:
            service = _describe_service(cloud, args.service, entry["region"])
            serving, latest = _traffic(service)
            if not latest or serving != entry["baseline_revision"]:
                raise
            percent = 0
        if percent not in _allowed_percent(entry):
            raise ValueError("legacy rollback found unowned traffic drift")
        percents[entry["region"]] = percent
    for entry in journal["regions"]:
        percent = percents[entry["region"]]
        entry["rollback_percent"] = percent
        _set_state(path, journal, entry["region"], "rollback_intent")
        must_restore = bool(percent)
        if not must_restore:
            service = _describe_service(cloud, args.service, entry["region"])
            serving, latest = _traffic(service)
            must_restore = latest or serving != entry["baseline_revision"]
        if must_restore:
            cloud.run(
                "run",
                "services",
                "update-traffic",
                args.service,
                f"--region={entry['region']}",
                f"--to-revisions={entry['baseline_revision']}=100",
                "--clear-tags",
                "--quiet",
                check=False,
            )
        _verify_allocation(
            cloud,
            args.service,
            entry["region"],
            entry["baseline_revision"],
            entry["candidate_revision"],
            0,
        )
        _set_state(path, journal, entry["region"], "rolled_back")


def _deploy(args: argparse.Namespace, cloud: Cloud, journal: dict[str, Any], path: Path) -> None:
    for entry in journal["regions"]:
        _deploy_entry(args, cloud, journal, path, entry)
    phases = ((10, "deployed", 0), (50, "ramp10", 10), (100, "ramp50", 50))
    for percent, prior_state, prior_percent in phases:
        for entry in journal["regions"]:
            state = entry["state"]
            target_state = f"ramp{percent}"
            intent_state = f"ramp{percent}_intent"
            if state == target_state:
                _verify_candidate(args, cloud, journal, entry)
                _verify_allocation(
                    cloud,
                    args.service,
                    entry["region"],
                    entry["baseline_revision"],
                    entry["candidate_revision"],
                    percent,
                )
                continue
            if state not in {prior_state, intent_state}:
                continue
            _verify_baseline(args, cloud, journal, entry)
            if state == prior_state:
                _set_state(path, journal, entry["region"], intent_state)
            current_percent = _current_candidate_percent(args, cloud, entry)
            if current_percent not in {prior_percent, percent}:
                raise ValueError("legacy traffic changed outside the journaled ramp")
            if current_percent != percent:
                traffic = f"{entry['candidate_revision']}={percent}"
                if percent < 100:
                    traffic += f",{entry['baseline_revision']}={100 - percent}"
                cloud.run(
                    "run",
                    "services",
                    "update-traffic",
                    args.service,
                    f"--region={entry['region']}",
                    f"--to-revisions={traffic}",
                    "--clear-tags",
                    "--quiet",
                    check=False,
                )
            _verify_candidate(args, cloud, journal, entry)
            _verify_allocation(
                cloud,
                args.service,
                entry["region"],
                entry["baseline_revision"],
                entry["candidate_revision"],
                percent,
            )
            _set_state(path, journal, entry["region"], target_state)


def _artifact(args: argparse.Namespace, cloud: Cloud, journal: dict[str, Any]) -> dict[str, Any]:
    entries = []
    for entry in journal["regions"]:
        if entry["state"] != "ramp100":
            raise ValueError("legacy hardening journal did not settle every region")
        _verify_candidate(args, cloud, journal, entry)
        _verify_allocation(
            cloud,
            args.service,
            entry["region"],
            entry["baseline_revision"],
            entry["candidate_revision"],
            100,
        )
        service = _describe_service(cloud, args.service, entry["region"])
        entries.append(
            {
                "region": entry["region"],
                "serving_revision": entry["candidate_revision"],
                "service_sha256": _sha(_service_semantic(service)),
                "revision_sha256": entry["expected_candidate_revision_sha256"],
                "iam_sha256": _iam(cloud, args.service, entry["region"]),
            }
        )
    return {
        "schema_version": 1,
        "kind": "trusted-router-legacy-hardening-artifact",
        "project_id": args.project,
        "service": args.service,
        "runtime_service_account": args.runtime_service_account,
        "operation_id": args.operation_id,
        "revision_suffix": journal["revision_suffix"],
        "regions": entries,
        "secret_refs": journal["secret_refs"],
        "journal_sha256": _file_sha(Path(f"{args.artifact}.state")),
        "created_at": datetime.now(UTC).isoformat(),
    }


def _verify_artifact(args: argparse.Namespace, cloud: Cloud, regions: list[str]) -> None:
    path = args.verify_artifact
    if path.is_symlink() or not path.is_file():
        raise ValueError("legacy hardening artifact must be a regular file")
    if path.stat().st_mode & 0o777 != 0o600:
        raise ValueError("legacy hardening artifact must be mode 0600")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != ARTIFACT_FIELDS:
        raise ValueError("legacy hardening artifact fields differ")
    if (
        value["schema_version"] != 1
        or value["kind"] != "trusted-router-legacy-hardening-artifact"
        or value["project_id"] != args.project
        or value["service"] != args.service
        or value["runtime_service_account"] != args.runtime_service_account
        or not re.fullmatch(r"[A-Za-z0-9._:-]{8,160}", str(value["operation_id"]))
        or not NAME_RE.fullmatch(str(value["revision_suffix"]))
        or not re.fullmatch(r"[0-9a-f]{64}", str(value["journal_sha256"]))
    ):
        raise ValueError("legacy hardening artifact identity differs")
    _validate_timestamp(value["created_at"])
    _validate_secret_refs(value["secret_refs"])
    if (
        not isinstance(value["regions"], list)
        or any(not isinstance(item, dict) for item in value["regions"])
        or [item.get("region") for item in value["regions"]] != regions
    ):
        raise ValueError("legacy hardening artifact region inventory differs")
    journal = {
        "revision_suffix": value["revision_suffix"],
        "secret_refs": value["secret_refs"],
    }
    for record in value["regions"]:
        if not isinstance(record, dict) or set(record) != ARTIFACT_REGION_FIELDS:
            raise ValueError("legacy hardening artifact regional fields differ")
        if record["serving_revision"] != f"{args.service}-{value['revision_suffix']}" or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(record[field]))
            for field in ("service_sha256", "revision_sha256", "iam_sha256")
        ):
            raise ValueError("legacy hardening artifact regional contract is invalid")
        revision = _describe_revision(cloud, record["serving_revision"], record["region"])
        entry = {
            "region": record["region"],
            "candidate_revision": record["serving_revision"],
            "expected_candidate_revision_sha256": record["revision_sha256"],
            "image": _revision_container(revision)["image"],
            "iam_sha256": record["iam_sha256"],
        }
        _verify_candidate(args, cloud, journal, entry)
        service = _describe_service(cloud, args.service, record["region"])
        if (
            _sha(_service_semantic(service)) != record["service_sha256"]
            or _iam(cloud, args.service, record["region"]) != record["iam_sha256"]
        ):
            raise ValueError("legacy hardening live postcondition drifted")
        serving, latest = _traffic(service)
        if latest or serving != record["serving_revision"]:
            raise ValueError("legacy hardening serving traffic drifted")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--regions", required=True)
    parser.add_argument("--runtime-service-account", required=True)
    parser.add_argument(
        "--operation-id", default=os.environ.get("TR_ROLLOUT_OPERATION_ID", "")
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--artifact", type=Path)
    mode.add_argument("--verify-artifact", type=Path)
    parser.add_argument(
        "--revision-suffix", default=os.environ.get("TR_LEGACY_HARDENING_REVISION_SUFFIX", "")
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    try:
        regions = _validate_inputs(args)
        cloud = Cloud(args.project)
        if args.verify_artifact:
            _verify_artifact(args, cloud, regions)
            return
        assert args.artifact is not None
        state_path = Path(f"{args.artifact}.state")
        if args.artifact.exists():
            raise ValueError("legacy hardening artifact already exists; use verify mode")
        if state_path.exists():
            if state_path.is_symlink() or not state_path.is_file():
                raise ValueError("legacy hardening state must be a regular file")
            journal = json.loads(state_path.read_text(encoding="utf-8"))
            journal = _validate_journal(args, journal, regions, path=state_path)
            suffix = journal.get("revision_suffix")
            if args.revision_suffix and args.revision_suffix != suffix:
                raise ValueError("legacy hardening resume suffix differs")
        else:
            suffix = args.revision_suffix
            if not NAME_RE.fullmatch(suffix) or len(f"{args.service}-{suffix}") > 63:
                raise ValueError("legacy hardening requires a canonical explicit revision suffix")
            journal = _capture_plan(args, cloud, regions, suffix)
            _atomic(state_path, journal, exclusive=True)
            journal = _validate_journal(args, journal, regions, path=state_path)
        if any(entry["state"] == "rollback_intent" for entry in journal["regions"]):
            _rollback(args, cloud, journal, state_path)
        try:
            _deploy(args, cloud, journal, state_path)
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            try:
                _rollback(args, cloud, journal, state_path)
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                json.JSONDecodeError,
            ) as rollback_error:
                raise ValueError(
                    "legacy hardening failed and automatic traffic rollback did not settle"
                ) from rollback_error
            raise ValueError(
                "legacy hardening failed; exact baseline traffic was restored"
            ) from error
        _atomic(args.artifact, _artifact(args, cloud, journal), exclusive=True)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
