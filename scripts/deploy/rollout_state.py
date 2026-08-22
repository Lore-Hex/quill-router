#!/usr/bin/env python3
"""Non-secret state handling for the six-surface Cloud Run rollout.

The rollout manifest is deliberately a recovery record, not a rendered deploy
specification.  Environment values and Secret Manager references stay out of
it; immutable postcondition hashes are enough to detect revision drift during
promotion and rollback.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SURFACES = ("public", "actions", "console", "chat", "webhooks", "internal")
OUTPUT_ONLY_URL_MAP_KEYS = {
    "creationTimestamp",
    "fingerprint",
    "id",
    "kind",
    "selfLink",
}
TOP_LEVEL_FIELDS = {
    "schema_version",
    "rollout_mode",
    "project_id",
    "image",
    "release",
    "created_at",
    "regions",
    "primary_region",
    "gateway_regions",
    "internal_regions",
    "bootstrap_artifact_sha256",
    "legacy_hardening_artifact_sha256",
    "frontend_attestation_sha256",
    "legacy_fallback",
    "domains",
    "preserved_hosts",
    "url_map",
    "promotion_state",
    "services",
}
URL_MAP_FIELDS = {
    "name",
    "https_proxy",
    "prior_snapshot",
    "candidate_snapshot",
    "prior_sha256",
    "candidate_sha256",
}
SERVICE_FIELDS = {
    "surface",
    "name",
    "region",
    "prior_exists",
    "prior_traffic",
    "adopted_bootstrap",
    "candidate_revision",
    "runtime_service_account",
    "ingress",
    "default_url_disabled",
    "concurrency",
    "min_instances",
    "service_max_instances",
    "revision_max_instances",
    "timeout_seconds",
    "memory",
    "cpu",
    "container_port",
    "vpc_network",
    "vpc_subnet",
    "vpc_egress",
    "startup_probe_path",
    "startup_probe_initial_delay_seconds",
    "startup_probe_timeout_seconds",
    "startup_probe_period_seconds",
    "startup_probe_failure_threshold",
    "max_request_body_bytes",
    "max_in_flight_request_body_bytes",
    "max_concurrent_request_bodies",
    "request_body_read_timeout_seconds",
    "postcondition_sha256",
}
TRAFFIC_FIELDS = {"revision", "resolved_revision", "latest_revision", "percent", "tag"}
LEGACY_FALLBACK_FIELDS = {
    "service",
    "backend",
    "region",
    "generation",
    "serving_revision",
    "serving_revision_sha256",
    "traffic",
    "postcondition_sha256",
    "backend_postcondition_sha256",
    "invoker_iam_sha256",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
RESOURCE_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,61}\Z")
REVISION_NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,61}\Z")
TAG_RE = re.compile(r"[a-z][a-z0-9-]{0,61}\Z")
SERVICE_ACCOUNT_RE = re.compile(
    r"[a-z][a-z0-9-]{4,28}[a-z0-9]@[a-z][a-z0-9-]{4,28}[a-z0-9][.]iam[.]gserviceaccount[.]com\Z"
)
REQUIRED_PRESERVED_HOSTS = {
    "api.trustedrouter.com",
    "api.allyrouter.com",
    "api.uptimerouter.com",
    "api.quillrouter.com",
    "api-aws.trustedrouter.com",
    "api-azure.trustedrouter.com",
    "api-azure-nz.trustedrouter.com",
    "api-azure-sea.trustedrouter.com",
    "api-eu-west-1.trustedrouter.com",
    "aws.trustedrouter.com",
    "azure.trustedrouter.com",
}
FORBIDDEN_MANIFEST_KEY_RE = re.compile(
    r"(?:^|_)(?:env|secret|token|password|credential|client_secret|private_key)(?:_|$)",
    re.IGNORECASE,
)
FORBIDDEN_MANIFEST_VALUE_RE = re.compile(
    r"(?:projects/[^\s/]+/secrets/|secretKeyRef|:latest\b|versions/(?:latest|\d+)|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|GCP_SERVICE_ACCOUNT_KEY_JSON|"
    r"(?:^|[^A-Za-z0-9])(?:sk|rk)_(?:live|test)_[A-Za-z0-9]|"
    r"(?:^|[^A-Za-z0-9])(?:AKIA[0-9A-Z]{12,}|ASIA[0-9A-Z]{12,}|"
    r"AIza[0-9A-Za-z_-]{20,}|gh[pousr]_[0-9A-Za-z]{20,}|"
    r"xox[baprs]-[0-9A-Za-z-]{10,}|ya29[.][0-9A-Za-z_-]{20,})|"
    r"(?:^|[^A-Za-z0-9])TR_[A-Z0-9_]+=)",
    re.IGNORECASE,
)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _strip_url_map_output(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_url_map_output(item)
            for key, item in value.items()
            if key not in OUTPUT_ONLY_URL_MAP_KEYS
        }
    if isinstance(value, list):
        return [_strip_url_map_output(item) for item in value]
    return value


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _manifest_strings(value: Any, path: tuple[str, ...] = ()) -> list[tuple[tuple[str, ...], str]]:
    strings: list[tuple[tuple[str, ...], str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if FORBIDDEN_MANIFEST_KEY_RE.search(str(key)):
                raise ValueError(f"manifest contains forbidden field {'.'.join((*path, str(key)))}")
            strings.extend(_manifest_strings(item, (*path, str(key))))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            strings.extend(_manifest_strings(item, (*path, str(index))))
    elif isinstance(value, str):
        strings.append((path, value))
    return strings


def _validate_prior_traffic_state(
    traffic_state: Any,
    *,
    prior_exists: bool,
) -> list[dict[str, Any]]:
    """Validate that captured Cloud Run traffic can be restored exactly."""
    if not isinstance(traffic_state, list):
        raise ValueError("prior_traffic must be a list")
    traffic_targets: set[tuple[str, str | None]] = set()
    traffic_tags: set[str] = set()
    positive_total = 0
    for traffic in traffic_state:
        if not isinstance(traffic, dict) or set(traffic) != TRAFFIC_FIELDS:
            raise ValueError("prior_traffic fields differ from schema v1")
        revision = traffic["revision"]
        resolved_revision = traffic["resolved_revision"]
        latest_revision = traffic["latest_revision"]
        percent = traffic["percent"]
        tag = traffic["tag"]
        if not isinstance(latest_revision, bool):
            raise ValueError("traffic latest_revision must be boolean")
        if latest_revision:
            raise ValueError("captured traffic must not contain floating LATEST traffic or tags")
        if not isinstance(revision, str) or not REVISION_NAME_RE.fullmatch(revision):
            raise ValueError("traffic revision must be a canonical name")
        if resolved_revision != revision:
            raise ValueError("named traffic target resolved_revision must equal revision")
        if not isinstance(resolved_revision, str) or not REVISION_NAME_RE.fullmatch(
            resolved_revision
        ):
            raise ValueError("traffic resolved_revision must be canonical")
        if isinstance(percent, bool) or not isinstance(percent, int) or not 0 <= percent <= 100:
            raise ValueError("traffic percent must be an integer from 0 through 100")
        if tag is not None:
            if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
                raise ValueError("traffic tag must be null or a canonical name")
            if percent > 0:
                raise ValueError(
                    "positive traffic targets must be untagged; tags use separate "
                    "zero-percent targets"
                )
            if tag in traffic_tags:
                raise ValueError("prior traffic tags must be unique")
            traffic_tags.add(tag)
        elif percent == 0:
            raise ValueError(
                "untagged zero-percent traffic targets are not exactly restorable"
            )
        traffic_target = (revision, tag)
        if traffic_target in traffic_targets:
            raise ValueError("prior traffic targets must be unique")
        traffic_targets.add(traffic_target)
        if percent > 0:
            positive_total += percent
    if prior_exists:
        if not traffic_state or positive_total != 100:
            raise ValueError("preexisting services require exact prior traffic summing to 100")
    elif traffic_state:
        raise ValueError("previously absent services must have empty prior traffic")
    return traffic_state


def validate_manifest(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    unknown = set(value) - TOP_LEVEL_FIELDS
    missing = TOP_LEVEL_FIELDS - set(value)
    if unknown or missing:
        raise ValueError(
            f"manifest top-level fields differ: missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    if value["schema_version"] != 1:
        raise ValueError("manifest schema_version must be 1")
    if value["rollout_mode"] not in {"initial_split", "existing_split"}:
        raise ValueError("manifest rollout_mode must be initial_split or existing_split")
    bootstrap_artifact_sha256 = value["bootstrap_artifact_sha256"]
    legacy_hardening_artifact_sha256 = value["legacy_hardening_artifact_sha256"]
    frontend_attestation_sha256 = value["frontend_attestation_sha256"]
    if not isinstance(frontend_attestation_sha256, str) or not SHA256_RE.fullmatch(
        frontend_attestation_sha256
    ):
        raise ValueError("manifest must bind the verified frontend attestation sha256")
    if value["rollout_mode"] == "initial_split":
        if not isinstance(bootstrap_artifact_sha256, str) or not SHA256_RE.fullmatch(
            bootstrap_artifact_sha256
        ):
            raise ValueError(
                "initial_split must bind the verified bootstrap artifact sha256"
            )
        if not isinstance(legacy_hardening_artifact_sha256, str) or not SHA256_RE.fullmatch(
            legacy_hardening_artifact_sha256
        ):
            raise ValueError(
                "initial_split must bind the verified legacy-hardening artifact sha256"
            )
    elif bootstrap_artifact_sha256 is not None:
        raise ValueError("existing_split must not carry a bootstrap artifact digest")
    elif legacy_hardening_artifact_sha256 is not None:
        raise ValueError("existing_split must not carry a legacy-hardening artifact digest")
    project_id = value["project_id"]
    if not isinstance(project_id, str) or not re.fullmatch(
        r"[a-z][a-z0-9-]{4,28}[a-z0-9]", project_id
    ):
        raise ValueError("manifest project_id is not a canonical GCP project id")
    if not isinstance(value["image"], str) or not re.fullmatch(
        r"[^\s,|@]{1,430}@sha256:[0-9a-f]{64}", value["image"]
    ):
        raise ValueError("manifest image must be an immutable sha256 digest reference")
    if not isinstance(value["release"], str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value["release"]):
        raise ValueError("manifest release is invalid")
    if not isinstance(value["created_at"], str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.]\d+)?(?:Z|[+-]\d{2}:\d{2})",
        value["created_at"],
    ):
        raise ValueError("manifest created_at must be an ISO-8601 timestamp")
    regions = value["regions"]
    if (
        not isinstance(regions, list)
        or not regions
        or len(regions) != len(set(regions))
        or any(not isinstance(region, str) or not re.fullmatch(r"[a-z]+-[a-z0-9]+[0-9]", region) for region in regions)
    ):
        raise ValueError("manifest regions must be a non-empty unique list")
    gateway_regions = value["gateway_regions"]
    if (
        not isinstance(gateway_regions, list)
        or not gateway_regions
        or len(gateway_regions) != len(set(gateway_regions))
        or any(
            not isinstance(region, str)
            or not re.fullmatch(r"[a-z]+-[a-z0-9]+[0-9]", region)
            for region in gateway_regions
        )
    ):
        raise ValueError("manifest gateway_regions must be a non-empty unique list")
    internal_regions = value["internal_regions"]
    if (
        not isinstance(internal_regions, list)
        or not internal_regions
        or len(internal_regions) != len(set(internal_regions))
        or any(
            not isinstance(region, str)
            or not re.fullmatch(r"[a-z]+-[a-z0-9]+[0-9]", region)
            for region in internal_regions
        )
        or not set(regions).issubset(internal_regions)
    ):
        raise ValueError(
            "manifest internal_regions must be unique and include every control region"
        )
    if value["primary_region"] != regions[0] or value["primary_region"] not in gateway_regions:
        raise ValueError(
            "manifest primary_region must be the first control region and a gateway region"
        )
    legacy_fallback = value["legacy_fallback"]
    if not isinstance(legacy_fallback, list):
        raise ValueError("manifest legacy_fallback must be a list")
    if value["rollout_mode"] == "existing_split":
        if legacy_fallback:
            raise ValueError("existing_split must not carry a legacy fallback cohort")
    else:
        seen_legacy_regions: set[str] = set()
        for index, entry in enumerate(legacy_fallback):
            if not isinstance(entry, dict) or set(entry) != LEGACY_FALLBACK_FIELDS:
                raise ValueError(
                    f"manifest legacy_fallback[{index}] fields differ from schema v1"
                )
            if entry["service"] != "trusted-router":
                raise ValueError("initial fallback service must be the legacy monolith")
            if entry["backend"] != "trusted-router-control-backend":
                raise ValueError("initial fallback backend must remain the legacy control backend")
            region = entry["region"]
            if region not in regions or region in seen_legacy_regions:
                raise ValueError("initial fallback regional inventory is inexact")
            seen_legacy_regions.add(region)
            generation = entry["generation"]
            if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
                raise ValueError("initial fallback generation must be a positive integer")
            revision = entry["serving_revision"]
            if (
                not isinstance(revision, str)
                or not REVISION_NAME_RE.fullmatch(revision)
                or not revision.startswith("trusted-router-")
            ):
                raise ValueError("initial fallback serving revision is invalid")
            traffic = _validate_prior_traffic_state(entry["traffic"], prior_exists=True)
            if (
                len(traffic) != 1
                or traffic[0]["resolved_revision"] != revision
                or traffic[0]["percent"] != 100
                or traffic[0]["tag"] is not None
            ):
                raise ValueError(
                    "initial fallback must be one named untagged 100%-serving revision"
                )
            if not SHA256_RE.fullmatch(str(entry["postcondition_sha256"])):
                raise ValueError("initial fallback postcondition hash is invalid")
            if not SHA256_RE.fullmatch(str(entry["serving_revision_sha256"])):
                raise ValueError("initial fallback serving revision hash is invalid")
            if not SHA256_RE.fullmatch(
                str(entry["backend_postcondition_sha256"])
            ):
                raise ValueError("initial fallback backend hash is invalid")
            if not SHA256_RE.fullmatch(str(entry["invoker_iam_sha256"])):
                raise ValueError("initial fallback invoker IAM hash is invalid")
        if seen_legacy_regions != set(regions):
            raise ValueError("initial fallback must cover every control region exactly")
    if value["domains"] != [
        "trustedrouter.com",
        "allyrouter.com",
        "uptimerouter.com",
    ]:
        raise ValueError("manifest must pin the canonical three-domain inventory")
    preserved_hosts = value["preserved_hosts"]
    if (
        not isinstance(preserved_hosts, list)
        or not preserved_hosts
        or len(preserved_hosts) != len(set(preserved_hosts))
        or any(
            not isinstance(host, str)
            or not re.fullmatch(
                r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:[.][a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+",
                host,
            )
            for host in preserved_hosts
        )
    ):
        raise ValueError("manifest preserved_hosts must be a non-empty unique list")
    if not REQUIRED_PRESERVED_HOSTS.issubset(preserved_hosts):
        raise ValueError("manifest preserved_hosts omits a required API/AWS/Azure host")
    required_regional_aliases = {
        f"api-{region}.quillrouter.com"
        for region in set(regions) | set(gateway_regions)
    }
    if not required_regional_aliases.issubset(preserved_hosts):
        raise ValueError("manifest preserved_hosts omits a regional quillrouter API alias")
    url_map = value["url_map"]
    if not isinstance(url_map, dict) or set(url_map) != URL_MAP_FIELDS:
        raise ValueError("manifest url_map fields differ from schema v1")
    if not isinstance(url_map["name"], str) or not RESOURCE_NAME_RE.fullmatch(url_map["name"]):
        raise ValueError("manifest URL-map name is invalid")
    if not isinstance(url_map["https_proxy"], str) or not RESOURCE_NAME_RE.fullmatch(
        url_map["https_proxy"]
    ):
        raise ValueError("manifest HTTPS proxy name is invalid")
    for field in ("prior_snapshot", "candidate_snapshot"):
        snapshot = Path(url_map[field])
        if snapshot.is_absolute() or ".." in snapshot.parts or snapshot.name != url_map[field]:
            raise ValueError(f"manifest {field} must be a sibling filename")
    if url_map["prior_snapshot"] == url_map["candidate_snapshot"]:
        raise ValueError("manifest URL-map snapshots must use distinct sibling files")
    if value["promotion_state"] != "promotion-state.json":
        raise ValueError("manifest promotion_state must be promotion-state.json")
    for field in ("prior_sha256", "candidate_sha256"):
        if not isinstance(url_map[field], str) or not SHA256_RE.fullmatch(url_map[field]):
            raise ValueError(f"manifest url_map.{field} is not a sha256 digest")

    services = value["services"]
    expected_pairs = {
        (surface, region)
        for surface in SURFACES
        for region in (internal_regions if surface == "internal" else regions)
    }
    actual_pairs: set[tuple[str, str]] = set()
    if not isinstance(services, list):
        raise ValueError("manifest services must be a list")
    names_by_surface: dict[str, str] = {}
    accounts_by_surface: dict[str, str] = {}
    contracts_by_surface: dict[str, tuple[Any, ...]] = {}
    platform_contracts_by_surface: dict[str, tuple[Any, ...]] = {}
    for index, service in enumerate(services):
        if not isinstance(service, dict) or set(service) != SERVICE_FIELDS:
            raise ValueError(f"manifest services[{index}] fields differ from schema v1")
        pair = (service["surface"], service["region"])
        if pair in actual_pairs:
            raise ValueError(f"duplicate manifest service entry {pair!r}")
        actual_pairs.add(pair)
        if not isinstance(service["prior_exists"], bool):
            raise ValueError(f"manifest services[{index}].prior_exists must be boolean")
        if not isinstance(service["adopted_bootstrap"], bool):
            raise ValueError(
                f"manifest services[{index}].adopted_bootstrap must be boolean"
            )
        try:
            _validate_prior_traffic_state(
                service["prior_traffic"], prior_exists=service["prior_exists"]
            )
        except ValueError as error:
            raise ValueError(f"manifest services[{index}] {error}") from error
        for field in (
            "concurrency",
            "min_instances",
            "service_max_instances",
            "revision_max_instances",
            "timeout_seconds",
            "max_request_body_bytes",
            "max_in_flight_request_body_bytes",
            "max_concurrent_request_bodies",
            "request_body_read_timeout_seconds",
            "cpu",
            "container_port",
            "startup_probe_initial_delay_seconds",
            "startup_probe_timeout_seconds",
            "startup_probe_period_seconds",
            "startup_probe_failure_threshold",
        ):
            if isinstance(service[field], bool) or not isinstance(service[field], int) or service[field] < 0:
                raise ValueError(f"manifest services[{index}].{field} must be non-negative")
        surface = service["surface"]
        name = service["name"]
        account = service["runtime_service_account"]
        if not isinstance(name, str) or not RESOURCE_NAME_RE.fullmatch(name):
            raise ValueError(f"manifest services[{index}].name is invalid")
        if not isinstance(account, str) or not SERVICE_ACCOUNT_RE.fullmatch(account):
            raise ValueError(f"manifest services[{index}].runtime_service_account is invalid")
        if not account.endswith(f"@{project_id}.iam.gserviceaccount.com"):
            raise ValueError("runtime service account must belong to manifest project")
        if not str(service["candidate_revision"]).startswith(f"{name}-") or not REVISION_NAME_RE.fullmatch(
            str(service["candidate_revision"])
        ):
            raise ValueError(f"manifest services[{index}].candidate_revision is invalid")
        if service["ingress"] != "internal-and-cloud-load-balancing":
            raise ValueError("all manifest services must pin LB-only ingress")
        if not isinstance(service["default_url_disabled"], bool):
            raise ValueError("manifest default_url_disabled must be boolean")
        if service["default_url_disabled"] != (surface != "internal"):
            raise ValueError("only internal may retain its default Cloud Run URL")
        if service["concurrency"] <= 0 or service["service_max_instances"] <= 0:
            raise ValueError("manifest concurrency and service max must be positive")
        if service["revision_max_instances"] != service["service_max_instances"]:
            raise ValueError("service and revision max-instance caps must match")
        if service["max_request_body_bytes"] <= 0 or service["max_in_flight_request_body_bytes"] < service["max_request_body_bytes"]:
            raise ValueError("manifest request-body budgets are invalid")
        if service["max_concurrent_request_bodies"] <= 0 or service["request_body_read_timeout_seconds"] <= 0:
            raise ValueError("manifest request-body concurrency/timeout must be positive")
        if not isinstance(service["memory"], str) or not re.fullmatch(
            r"[1-9][0-9]*(?:Mi|Gi)", service["memory"]
        ):
            raise ValueError("manifest memory must be a canonical Cloud Run quantity")
        for field in ("vpc_network", "vpc_subnet"):
            if not isinstance(service[field], str) or not RESOURCE_NAME_RE.fullmatch(
                service[field]
            ):
                raise ValueError(f"manifest {field} must be a canonical resource name")
        if service["vpc_egress"] != "private-ranges-only":
            raise ValueError("manifest VPC egress must be private-ranges-only")
        if service["startup_probe_path"] != "/ready":
            raise ValueError("manifest startup probe must use /ready")
        names_by_surface.setdefault(surface, name)
        accounts_by_surface.setdefault(surface, account)
        if names_by_surface[surface] != name or accounts_by_surface[surface] != account:
            raise ValueError("service name/account must be stable across regions")
        contract = tuple(
            service[field]
            for field in (
                "ingress",
                "default_url_disabled",
                "concurrency",
                "min_instances",
                "service_max_instances",
                "revision_max_instances",
                "timeout_seconds",
                "max_request_body_bytes",
                "max_in_flight_request_body_bytes",
                "max_concurrent_request_bodies",
                "request_body_read_timeout_seconds",
            )
        )
        contracts_by_surface.setdefault(surface, contract)
        if contracts_by_surface[surface] != contract:
            raise ValueError("surface runtime contract must be stable across regions")
        platform_contract = tuple(
            service[field]
            for field in (
                "memory",
                "cpu",
                "container_port",
                "vpc_network",
                "vpc_subnet",
                "vpc_egress",
                "startup_probe_path",
                "startup_probe_initial_delay_seconds",
                "startup_probe_timeout_seconds",
                "startup_probe_period_seconds",
                "startup_probe_failure_threshold",
            )
        )
        platform_contracts_by_surface.setdefault(surface, platform_contract)
        if platform_contracts_by_surface[surface] != platform_contract:
            raise ValueError("surface platform contract must be stable across regions")
        if not SHA256_RE.fullmatch(str(service["postcondition_sha256"])):
            raise ValueError(f"manifest services[{index}].postcondition_sha256 is invalid")
    if actual_pairs != expected_pairs:
        missing_pairs = sorted(expected_pairs - actual_pairs)
        extra_pairs = sorted(actual_pairs - expected_pairs)
        raise ValueError(
            f"manifest must contain one service per surface and region: "
            f"missing={missing_pairs}, extra={extra_pairs}"
        )
    if len(set(names_by_surface.values())) != len(SURFACES):
        raise ValueError("the six surfaces must use distinct Cloud Run service names")
    if len(set(accounts_by_surface.values())) != len(SURFACES):
        raise ValueError("the six surfaces must use distinct runtime service accounts")
    expected_names = {
        "public": "trusted-router-public",
        "actions": "trusted-router-actions",
        "console": "trusted-router-console",
        "chat": "trusted-router-chat",
        "webhooks": "trusted-router-webhooks",
        "internal": "trusted-router-billing",
    }
    if names_by_surface != expected_names:
        raise ValueError("manifest service names differ from the canonical six-service inventory")
    expected_accounts = {
        surface: f"tr-{surface}@{project_id}.iam.gserviceaccount.com"
        for surface in SURFACES
    }
    if accounts_by_surface != expected_accounts:
        raise ValueError("manifest runtime identities differ from the canonical six-SA inventory")
    expected_contracts = {
        "public": ("internal-and-cloud-load-balancing", True, 4, 0, 10, 10, 60, 1048576, 4194304, 2, 10),
        "actions": ("internal-and-cloud-load-balancing", True, 4, 0, 2, 2, 30, 262144, 1048576, 2, 10),
        "console": ("internal-and-cloud-load-balancing", True, 4, 1, 20, 20, 300, 4194304, 16777216, 2, 30),
        "chat": ("internal-and-cloud-load-balancing", True, 2, 1, 20, 20, 300, 33554432, 67108864, 2, 30),
        "webhooks": ("internal-and-cloud-load-balancing", True, 4, 1, 10, 10, 60, 1048576, 4194304, 2, 10),
        "internal": ("internal-and-cloud-load-balancing", False, 8, 2, 50, 50, 300, 33554432, 67108864, 4, 30),
    }
    if contracts_by_surface != expected_contracts:
        raise ValueError("manifest runtime contracts differ from the reviewed six-surface constants")
    expected_platform_contracts = {
        "public": ("1Gi", 1, 8080, "default", "default", "private-ranges-only", "/ready", 0, 10, 10, 18),
        "actions": ("512Mi", 1, 8080, "default", "default", "private-ranges-only", "/ready", 0, 10, 10, 18),
        "console": ("2Gi", 1, 8080, "default", "default", "private-ranges-only", "/ready", 0, 10, 10, 18),
        "chat": ("2Gi", 1, 8080, "default", "default", "private-ranges-only", "/ready", 0, 10, 10, 18),
        "webhooks": ("1Gi", 1, 8080, "default", "default", "private-ranges-only", "/ready", 0, 10, 10, 18),
        "internal": ("2Gi", 1, 8080, "default", "default", "private-ranges-only", "/ready", 0, 10, 10, 18),
    }
    if platform_contracts_by_surface != expected_platform_contracts:
        raise ValueError(
            "manifest platform contracts differ from the reviewed six-surface constants"
        )
    if value["rollout_mode"] == "existing_split":
        if not all(service["prior_exists"] for service in services):
            raise ValueError("existing_split requires every service to preexist")
        if url_map["prior_sha256"] != url_map["candidate_sha256"]:
            raise ValueError("existing_split must preserve the URL map")
    else:
        if url_map["prior_sha256"] == url_map["candidate_sha256"]:
            raise ValueError("initial_split must produce a distinct candidate URL map")
        for surface in SURFACES:
            inventory = {
                service["prior_exists"]
                for service in services
                if service["surface"] == surface
            }
            expected_inventory = {surface == "internal"}
            if inventory != expected_inventory:
                raise ValueError(
                    "initial_split requires five absent web companions and the "
                    f"preexisting bootstrap internal cohort; {surface}={inventory!r}"
                )
    for service in services:
        prior_revisions = {
            traffic["resolved_revision"] for traffic in service["prior_traffic"]
        }
        if service["candidate_revision"] in prior_revisions:
            adopted_bootstrap = (
                value["rollout_mode"] == "initial_split"
                and service["surface"] == "internal"
                and service["adopted_bootstrap"]
                and service["prior_exists"]
                and len(service["prior_traffic"]) == 1
                and service["prior_traffic"][0]["resolved_revision"]
                == service["candidate_revision"]
                and service["prior_traffic"][0]["percent"] == 100
                and service["prior_traffic"][0]["tag"] is None
            )
            if not adopted_bootstrap:
                raise ValueError(
                    "candidate revision must differ from every prior traffic target"
                )

        should_adopt_bootstrap = (
            value["rollout_mode"] == "initial_split"
            and service["surface"] == "internal"
        )
        if service["adopted_bootstrap"] != should_adopt_bootstrap:
            raise ValueError(
                "only every initial internal entry may adopt the verified bootstrap"
            )

    for field_path, string in _manifest_strings(value):
        if FORBIDDEN_MANIFEST_VALUE_RE.search(string):
            raise ValueError(
                "manifest contains a secret-like value at " + ".".join(field_path)
            )
    return value


def _service_contract(service: dict[str, Any]) -> dict[str, Any]:
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


def _revision_contract(revision: dict[str, Any]) -> dict[str, Any]:
    """Return the immutable legacy revision fields needed for safe fallback."""
    metadata = revision.get("metadata") or {}
    return {
        "metadata": {
            "name": metadata.get("name"),
            "annotations": metadata.get("annotations") or {},
            "labels": metadata.get("labels") or {},
        },
        "spec": revision.get("spec") or {},
    }


def _iam_policy_contract(policy: dict[str, Any]) -> dict[str, Any]:
    bindings = []
    for binding in policy.get("bindings") or []:
        bindings.append(
            {
                "role": binding.get("role"),
                "members": sorted(binding.get("members") or []),
                "condition": binding.get("condition") or None,
            }
        )
    bindings.sort(
        key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":"))
    )
    return {"bindings": bindings}


def _traffic_state(service: dict[str, Any]) -> list[dict[str, Any]]:
    spec = service.get("spec") or {}
    status = service.get("status") or {}
    desired = list(spec.get("traffic") or [])
    observed = list(status.get("traffic") or [])
    if not desired:
        desired = observed
    if desired and not any(bool(target.get("latestRevision", False)) for target in desired):
        def named_state(targets: list[dict[str, Any]]) -> list[tuple[str, int, str | None]]:
            normalized: list[tuple[str, int, str | None]] = []
            for target in targets:
                revision = target.get("revisionName")
                if not isinstance(revision, str) or not revision:
                    raise ValueError(f"observed traffic target has no named revision: {target!r}")
                if bool(target.get("latestRevision", False)):
                    raise ValueError("observed traffic unexpectedly contains floating LATEST")
                normalized.append(
                    (
                        revision,
                        int(target.get("percent") or 0),
                        target.get("tag") or None,
                    )
                )
            return sorted(normalized, key=lambda item: (item[2] or "", item[0], item[1]))

        if named_state(desired) != named_state(observed):
            raise ValueError(
                "Cloud Run desired and observed named traffic targets have not converged"
            )
    result: list[dict[str, Any]] = []
    for target in desired:
        latest = bool(target.get("latestRevision", False))
        revision = target.get("revisionName")
        tag = target.get("tag") or None
        percent = int(target.get("percent") or 0)
        resolved = revision
        if latest:
            revision = None
            if tag is not None:
                candidates = {
                    item.get("revisionName")
                    for item in observed
                    if item.get("revisionName") and item.get("tag") == tag
                }
            else:
                candidates = {
                    item.get("revisionName")
                    for item in observed
                    if item.get("revisionName")
                    and not item.get("tag")
                    and int(item.get("percent") or 0) == percent
                }
            if resolved:
                candidates.add(resolved)
            if not candidates:
                latest_ready = status.get("latestReadyRevisionName")
                if latest_ready:
                    candidates.add(latest_ready)
            if len(candidates) != 1:
                raise ValueError(
                    f"cannot resolve floating LATEST traffic target: {target!r}"
                )
            resolved = next(iter(candidates))
        if not resolved:
            raise ValueError(f"traffic target has no resolved revision: {target!r}")
        result.append(
            {
                "revision": revision,
                "resolved_revision": resolved,
                "latest_revision": latest,
                "percent": percent,
                "tag": tag,
            }
        )
    return sorted(
        result,
        key=lambda item: (
            item["tag"] or "",
            item["latest_revision"],
            item["revision"] or "",
            item["percent"],
        ),
    )


def _service_generation(service: dict[str, Any]) -> dict[str, Any]:
    metadata = service.get("metadata") or {}
    generation = metadata.get("generation")
    resource_version = metadata.get("resourceVersion")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ValueError("service metadata.generation must be a positive integer")
    if not isinstance(resource_version, str) or not resource_version:
        raise ValueError("service metadata.resourceVersion must be a non-empty string")
    return {"generation": generation, "resourceVersion": resource_version}


def _validate_captured_service_traffic(path: Path) -> None:
    service = _read_json(path)
    traffic = _traffic_state(service)
    _validate_prior_traffic_state(traffic, prior_exists=True)


def _build_manifest(args: argparse.Namespace) -> None:
    services = [
        json.loads(line)
        for line in args.entries.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    manifest = {
        "schema_version": 1,
        "rollout_mode": args.rollout_mode,
        "project_id": args.project_id,
        "image": args.image,
        "release": args.release,
        "created_at": args.created_at,
        "regions": args.regions.split(","),
        "primary_region": args.primary_region,
        "gateway_regions": args.gateway_regions.split(","),
        "internal_regions": args.internal_regions.split(","),
        "bootstrap_artifact_sha256": args.bootstrap_artifact_sha256 or None,
        "legacy_hardening_artifact_sha256": (
            args.legacy_hardening_artifact_sha256 or None
        ),
        "frontend_attestation_sha256": args.frontend_attestation_sha256,
        "legacy_fallback": [
            json.loads(line)
            for line in args.legacy_fallback.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ],
        "domains": args.domains.split(","),
        "preserved_hosts": args.preserved_hosts.split(","),
        "url_map": {
            "name": args.url_map_name,
            "https_proxy": args.https_proxy,
            "prior_snapshot": args.prior_snapshot,
            "candidate_snapshot": args.candidate_snapshot,
            "prior_sha256": args.prior_sha256,
            "candidate_sha256": args.candidate_sha256,
        },
        "promotion_state": "promotion-state.json",
        "services": services,
    }
    validate_manifest(manifest)
    _atomic_write_json(args.manifest, manifest)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    sanitize = subparsers.add_parser("sanitize-url-map")
    sanitize.add_argument("input", type=Path)
    sanitize.add_argument("output", type=Path)

    hash_url_map = subparsers.add_parser("hash-url-map")
    hash_url_map.add_argument("input", type=Path)

    hash_service = subparsers.add_parser("hash-service")
    hash_service.add_argument("input", type=Path)

    hash_resource = subparsers.add_parser("hash-resource")
    hash_resource.add_argument("input", type=Path)

    hash_revision = subparsers.add_parser("hash-revision")
    hash_revision.add_argument("input", type=Path)

    hash_iam_policy = subparsers.add_parser("hash-iam-policy")
    hash_iam_policy.add_argument("input", type=Path)

    traffic_state = subparsers.add_parser("traffic-state")
    traffic_state.add_argument("input", type=Path)

    service_generation = subparsers.add_parser("service-generation")
    service_generation.add_argument("input", type=Path)

    validate_traffic = subparsers.add_parser("validate-prior-traffic")
    validate_traffic.add_argument("input", type=Path)

    validate = subparsers.add_parser("validate-manifest")
    validate.add_argument("manifest", type=Path)

    build = subparsers.add_parser("build-manifest")
    build.add_argument("--manifest", type=Path, required=True)
    build.add_argument("--entries", type=Path, required=True)
    build.add_argument("--rollout-mode", required=True)
    build.add_argument("--project-id", required=True)
    build.add_argument("--image", required=True)
    build.add_argument("--release", required=True)
    build.add_argument("--created-at", required=True)
    build.add_argument("--regions", required=True)
    build.add_argument("--primary-region", required=True)
    build.add_argument("--gateway-regions", required=True)
    build.add_argument("--internal-regions", required=True)
    build.add_argument("--bootstrap-artifact-sha256", required=True)
    build.add_argument("--legacy-hardening-artifact-sha256", required=True)
    build.add_argument("--frontend-attestation-sha256", required=True)
    build.add_argument("--legacy-fallback", type=Path, required=True)
    build.add_argument("--domains", required=True)
    build.add_argument("--preserved-hosts", required=True)
    build.add_argument("--url-map-name", required=True)
    build.add_argument("--https-proxy", required=True)
    build.add_argument("--prior-snapshot", required=True)
    build.add_argument("--candidate-snapshot", required=True)
    build.add_argument("--prior-sha256", required=True)
    build.add_argument("--candidate-sha256", required=True)
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "sanitize-url-map":
        _atomic_write_json(args.output, _strip_url_map_output(_read_json(args.input)))
    elif args.command == "hash-url-map":
        print(_sha256(_strip_url_map_output(_read_json(args.input))))
    elif args.command == "hash-service":
        print(_sha256(_service_contract(_read_json(args.input))))
    elif args.command == "hash-resource":
        print(_sha256(_strip_url_map_output(_read_json(args.input))))
    elif args.command == "hash-revision":
        print(_sha256(_revision_contract(_read_json(args.input))))
    elif args.command == "hash-iam-policy":
        print(_sha256(_iam_policy_contract(_read_json(args.input))))
    elif args.command == "traffic-state":
        print(json.dumps(_traffic_state(_read_json(args.input)), sort_keys=True))
    elif args.command == "service-generation":
        print(json.dumps(_service_generation(_read_json(args.input)), sort_keys=True))
    elif args.command == "validate-prior-traffic":
        _validate_captured_service_traffic(args.input)
    elif args.command == "validate-manifest":
        validate_manifest(_read_json(args.manifest))
    elif args.command == "build-manifest":
        _build_manifest(args)
    else:  # pragma: no cover - argparse owns command totality.
        raise AssertionError(args.command)


if __name__ == "__main__":
    main()
