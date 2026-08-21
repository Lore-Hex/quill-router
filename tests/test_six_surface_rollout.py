"""Executable contracts for the six-surface Cloud Run release transaction."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "quill-cloud-proxy"
STATE_TOOL = ROOT / "scripts/deploy/rollout_state.py"
HELPER = ROOT / "scripts/deploy/rollout_rollback.sh"
ROLLOUT = ROOT / "scripts/deploy/rollout.sh"
RECOVERY = ROOT / "scripts/deploy/rollout_recovery.sh"

SURFACES = ("public", "actions", "console", "chat", "webhooks", "internal")
SERVICES = {
    "public": "trusted-router-public",
    "actions": "trusted-router-actions",
    "console": "trusted-router-console",
    "chat": "trusted-router-chat",
    "webhooks": "trusted-router-webhooks",
    "internal": "trusted-router-billing",
}
EDGES = {
    "public": ("trusted-router-public-backend", "trusted-router-public-neg", "trusted-router-public-edge"),
    "actions": ("trusted-router-actions-backend", "trusted-router-actions-neg", "trusted-router-actions-edge"),
    "console": ("trusted-router-console-backend", "trusted-router-console-neg", "trusted-router-console-edge"),
    "chat": ("trusted-router-chat-backend", "trusted-router-chat-neg", "trusted-router-chat-edge"),
    "webhooks": ("trusted-router-webhooks-backend", "trusted-router-webhooks-neg", "trusted-router-webhooks-edge"),
    "internal": ("trusted-router-billing-backend", "trusted-router-billing-neg", "trusted-router-billing-edge"),
}
CONTRACTS = {
    "public": (4, 0, 10, 60, 1_048_576, 4_194_304, 2, 10),
    "actions": (4, 0, 2, 30, 262_144, 1_048_576, 2, 10),
    "console": (4, 1, 20, 300, 4_194_304, 16_777_216, 2, 30),
    "chat": (2, 1, 20, 300, 33_554_432, 67_108_864, 2, 30),
    "webhooks": (4, 1, 10, 60, 1_048_576, 4_194_304, 2, 10),
    "internal": (8, 2, 50, 300, 33_554_432, 67_108_864, 4, 30),
}
MEMORY = {
    "public": "1Gi",
    "actions": "512Mi",
    "console": "2Gi",
    "chat": "2Gi",
    "webhooks": "1Gi",
    "internal": "2Gi",
}
IMAGE = "us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/app@sha256:" + "1" * 64
STATE_GCS_URI = "gs://trusted-router-rollout-state/releases/test/promotion-state.json"
STATE_GCS_ROLE = "projects/quill-cloud-proxy/roles/trustedRouterRolloutJournal"
RECOVERY_PREFIX = "gs://trusted-router-rollout-state/recovery/quill-cloud-proxy"
RECOVERY_BUNDLE = f"{RECOVERY_PREFIX}/releases/test-epoch-0001"
RECOVERY_AUTHORITY = f"{RECOVERY_PREFIX}/authority.json"
FRONTEND_TOOL = ROOT / "scripts/deploy/rollout_frontend_attest.py"
SMOKE = ROOT / "scripts/deploy/rollout_smoke.sh"
FRONTEND_HOSTS = [
    "trustedrouter.com",
    "www.trustedrouter.com",
    "status.trustedrouter.com",
    "eu.trustedrouter.com",
    "status-us.trustedrouter.com",
    "status-eu.trustedrouter.com",
    "allyrouter.com",
    "www.allyrouter.com",
    "status.allyrouter.com",
    "trust.allyrouter.com",
    "uptimerouter.com",
    "www.uptimerouter.com",
    "status.uptimerouter.com",
    "trust.uptimerouter.com",
]
FRONTEND_VIP = "34.111.20.30"


def _run_state(*args: str | Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603
        [sys.executable, str(STATE_TOOL), *(str(arg) for arg in args)],
        check=check,
        capture_output=True,
        text=True,
    )


def _hash_json(path: Path, kind: str) -> str:
    return _run_state(kind, path).stdout.strip()


def _policy() -> dict[str, Any]:
    allowed = (
        r"^(trustedrouter[.]com|www[.]trustedrouter[.]com|status[.]trustedrouter[.]com|"
        r"trust[.]trustedrouter[.]com|eu[.]trustedrouter[.]com|status-us[.]trustedrouter[.]com|"
        r"status-eu[.]trustedrouter[.]com|allyrouter[.]com|www[.]allyrouter[.]com|"
        r"status[.]allyrouter[.]com|trust[.]allyrouter[.]com|uptimerouter[.]com|"
        r"www[.]uptimerouter[.]com|status[.]uptimerouter[.]com|"
        r"trust[.]uptimerouter[.]com)(:[0-9]+)?$"
    )

    def throttle(priority: int, count: int, *, expression: str | None, preview: bool) -> dict[str, Any]:
        match: dict[str, Any]
        if expression is None:
            match = {
                "config": {"srcIpRanges": ["*"]},
                "versionedExpr": "SRC_IPS_V1",
            }
        else:
            match = {"expr": {"expression": expression}}
        descriptions = {
            1000: "Browser inference proxy per-client throttle",
            1100: "State-changing request per-client throttle",
            1200: "All-path per-source safety ceiling",
        }
        return {
            "priority": priority,
            "action": "throttle",
            "description": descriptions[priority],
            "preview": preview,
            "match": match,
            "rateLimitOptions": {
                "rateLimitThreshold": {"count": count, "intervalSec": 60},
                "conformAction": "allow",
                "exceedAction": "deny(429)",
                "enforceOnKey": "IP",
            },
        }

    return {
        "type": "CLOUD_ARMOR",
        "description": (
            "TrustedRouter exact edge controls; host and all-path gates enforced, "
            "route-shape rules previewed"
        ),
        "rules": [
            {
                "priority": 900,
                "action": "deny(403)",
                "description": "Reject hosts outside canonical and marketing aliases",
                "preview": False,
                "match": {
                    "expr": {
                        "expression": "!has(request.headers['host']) || "
                        f"!request.headers['host'].lower().matches('{allowed}')"
                    }
                },
            },
            throttle(
                1000,
                120,
                expression="request.path.startsWith('/chat-proxy/')",
                preview=True,
            ),
            throttle(
                1100,
                300,
                expression=(
                    "request.method != 'GET' && request.method != 'HEAD' && "
                    "request.method != 'OPTIONS'"
                ),
                preview=True,
            ),
            throttle(1200, 2400, expression=None, preview=False),
            {
                "priority": 2_147_483_647,
                "action": "allow",
                "description": "Default allow; bounded route classes are evaluated first",
                "preview": False,
                "match": {
                    "config": {"srcIpRanges": ["*"]},
                    "versionedExpr": "SRC_IPS_V1",
                },
            },
        ]
    }


def _iam_policy(mapping: dict[str, str]) -> dict[str, Any]:
    bindings: dict[str, list[str]] = {}
    for surface, role in mapping.items():
        if role:
            bindings.setdefault(role, []).append(
                f"serviceAccount:tr-{surface}@quill-cloud-proxy.iam.gserviceaccount.com"
            )
    return {
        "bindings": [
            {"role": role, "members": members}
            for role, members in sorted(bindings.items())
        ]
    }


def _iam_fixture() -> dict[str, Any]:
    empty = {surface: "" for surface in SURFACES}
    return {
        "project": _iam_policy(
            {
                **{surface: "roles/serviceusage.serviceUsageConsumer" for surface in SURFACES},
                "actions": "",
            }
        ),
        "ancestors": [{"type": "project", "id": "quill-cloud-proxy"}],
        "spanner_instance": _iam_policy(empty),
        "spanner_database": _iam_policy(
            {
                "public": "roles/spanner.databaseReader",
                "actions": "",
                "console": "roles/spanner.databaseUser",
                "chat": "roles/spanner.databaseReader",
                "webhooks": "roles/spanner.databaseUser",
                "internal": "roles/spanner.databaseUser",
            }
        ),
        "bigtable_instance": _iam_policy(
            {
                "public": "roles/bigtable.reader",
                "actions": "",
                "console": "roles/bigtable.reader",
                "chat": "",
                "webhooks": "",
                "internal": "roles/bigtable.user",
            }
        ),
        "bigtable_table": _iam_policy(empty),
        "kms_keyring": _iam_policy(empty),
        "byok": _iam_policy(
            {
                "public": "",
                "actions": "",
                "console": "roles/cloudkms.cryptoKeyEncrypterDecrypter",
                "chat": "",
                "webhooks": "",
                "internal": "roles/cloudkms.cryptoKeyDecrypter",
            }
        ),
        "google_ads": _iam_policy(
            {
                **empty,
                "console": "roles/cloudkms.cryptoKeyEncrypter",
            }
        ),
        "secrets": [],
    }


def _service_json(surface: str, region: str) -> tuple[dict[str, Any], str, str]:
    service = SERVICES[surface]
    candidate = f"{service}-candidate"
    prior = f"{service}-prior"
    concurrency, minimum, maximum, timeout, body, inflight, body_slots, read_timeout = CONTRACTS[surface]
    annotations = {
        "run.googleapis.com/ingress": "internal-and-cloud-load-balancing",
        "run.googleapis.com/ingress-status": "internal-and-cloud-load-balancing",
        "run.googleapis.com/maxScale": str(maximum),
    }
    if surface != "internal":
        annotations["run.googleapis.com/default-url-disabled"] = "true"
    env = [
        {"name": "TR_SERVICE_SURFACE", "value": surface},
        {"name": "TR_RATE_LIMIT_CLIENT_IP_MODE", "value": "edge_header"},
        {"name": "TR_MAX_REQUEST_BODY_BYTES", "value": str(body)},
        {"name": "TR_MAX_IN_FLIGHT_REQUEST_BODY_BYTES", "value": str(inflight)},
        {"name": "TR_MAX_CONCURRENT_REQUEST_BODIES", "value": str(body_slots)},
        {"name": "TR_REQUEST_BODY_READ_TIMEOUT_SECONDS", "value": str(read_timeout)},
        {"name": "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS", "value": "0"},
        {"name": "TR_REMEDIATOR_IN_PROCESS_ENABLED", "value": "false"},
    ]
    data = {
        "metadata": {"name": service, "generation": 7, "annotations": annotations},
        "spec": {
            "traffic": [{"revisionName": prior, "percent": 100}],
            "template": {
                "metadata": {
                    "name": candidate,
                    "annotations": {
                        "autoscaling.knative.dev/minScale": str(minimum),
                        "autoscaling.knative.dev/maxScale": str(maximum),
                        "run.googleapis.com/network-interfaces": json.dumps(
                            [{"network": "default", "subnetwork": "default"}],
                            separators=(",", ":"),
                        ),
                        "run.googleapis.com/vpc-access-egress": "private-ranges-only",
                    },
                },
                "spec": {
                    "serviceAccountName": (
                        f"tr-{surface}@quill-cloud-proxy.iam.gserviceaccount.com"
                    ),
                    "containerConcurrency": concurrency,
                    "timeoutSeconds": f"{timeout}s",
                    "containers": [
                        {
                            "image": IMAGE,
                            "ports": [{"containerPort": 8080, "name": "http1"}],
                            "resources": {
                                "limits": {"cpu": "1000m", "memory": MEMORY[surface]}
                            },
                            "startupProbe": {
                                "httpGet": {"path": "/ready", "port": 8080},
                                "initialDelaySeconds": 0,
                                "timeoutSeconds": 10,
                                "periodSeconds": 10,
                                "failureThreshold": 18,
                            },
                            "env": env,
                        }
                    ],
                },
            },
        },
        "status": {
            "observedGeneration": 7,
            "latestCreatedRevisionName": candidate,
            "latestReadyRevisionName": candidate,
            "conditions": [{"type": "Ready", "status": "True"}],
            "url": "" if surface != "internal" else f"https://{service}-{region}.run.app",
            "traffic": [{"revisionName": prior, "percent": 100}],
        },
    }
    return data, candidate, prior


def _fixture(
    tmp_path: Path,
    mode: str = "existing_split",
    regions: list[str] | None = None,
    internal_regions: list[str] | None = None,
) -> tuple[Path, Path, Path]:
    regions = list(regions or ["us-central1", "europe-west4"])
    internal_regions = list(internal_regions or regions)
    prior_map = {
        "name": "trusted-router-map",
        "selfLink": (
            "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
            "global/urlMaps/trusted-router-map"
        ),
        "defaultService": "trusted-router-control-backend",
    }
    candidate_map = copy.deepcopy(prior_map)
    if mode == "initial_split":
        candidate_map["description"] = "six-surface candidate"
    prior_path = tmp_path / "url-map.prior.json"
    candidate_path = tmp_path / "url-map.candidate.json"
    prior_path.write_text(json.dumps(prior_map), encoding="utf-8")
    candidate_path.write_text(json.dumps(candidate_map), encoding="utf-8")
    prior_path.chmod(0o600)
    candidate_path.chmod(0o600)
    prior_hash = _hash_json(prior_path, "hash-url-map")
    candidate_hash = _hash_json(candidate_path, "hash-url-map")

    services: list[dict[str, Any]] = []
    live_services: dict[str, Any] = {}
    backends: dict[str, Any] = {}
    policies: dict[str, Any] = {}
    negs: dict[str, Any] = {}
    legacy_fallback: list[dict[str, Any]] = []
    for surface in SURFACES:
        concurrency, minimum, maximum, timeout, body, inflight, body_slots, read_timeout = CONTRACTS[surface]
        backend, neg, policy = EDGES[surface]
        backends[backend] = {
            "name": backend,
            "loadBalancingScheme": "EXTERNAL_MANAGED",
            "protocol": "HTTP",
            "timeoutSec": timeout,
            "securityPolicy": (
                "https://www.googleapis.com/compute/v1/projects/"
                f"quill-cloud-proxy/global/securityPolicies/{policy}"
            ),
            "customRequestHeaders": ["X-TrustedRouter-Client-IP:{client_ip_address}"],
            "customResponseHeaders": [],
            "iap": {"enabled": False},
            "logConfig": {
                "enable": True,
                "sampleRate": 0.1,
                "optionalMode": "EXCLUDE_ALL_OPTIONAL",
            },
            "enableCDN": surface == "public",
            "backends": [
                {
                    "group": (
                        f"https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                        f"regions/{region}/networkEndpointGroups/{neg}"
                    )
                }
                for region in regions
            ],
        }
        if surface == "public":
            backends[backend].update(
                {
                    "compressionMode": "AUTOMATIC",
                    "cdnPolicy": {
                        "cacheMode": "USE_ORIGIN_HEADERS",
                        "negativeCaching": False,
                        "serveWhileStale": 600,
                        "cacheKeyPolicy": {
                            "includeHost": True,
                            "includeProtocol": True,
                            "includeQueryString": True,
                            "queryStringBlacklist": [],
                            "queryStringWhitelist": [],
                        },
                    },
                }
            )
        policies[policy] = _policy()
        service_regions = internal_regions if surface == "internal" else regions
        for region in service_regions:
            service_json, candidate, prior = _service_json(surface, region)
            prior_exists = True
            prior_traffic = [
                {
                    "revision": prior,
                    "resolved_revision": prior,
                    "latest_revision": False,
                    "percent": 100,
                    "tag": None,
                }
            ]
            adopted_bootstrap = False
            if mode == "initial_split":
                service_json["spec"]["traffic"] = [
                    {"revisionName": candidate, "percent": 100}
                ]
                service_json["status"]["traffic"] = [
                    {"revisionName": candidate, "percent": 100}
                ]
                if surface == "internal":
                    prior = candidate
                    prior_traffic = [
                        {
                            "revision": candidate,
                            "resolved_revision": candidate,
                            "latest_revision": False,
                            "percent": 100,
                            "tag": None,
                        }
                    ]
                    adopted_bootstrap = True
                else:
                    prior_exists = False
                    prior_traffic = []
            service_path = tmp_path / f"service-{surface}-{region}.json"
            service_path.write_text(json.dumps(service_json), encoding="utf-8")
            postcondition = _hash_json(service_path, "hash-service")
            services.append(
                {
                    "surface": surface,
                    "name": SERVICES[surface],
                    "region": region,
                    "prior_exists": prior_exists,
                    "prior_traffic": prior_traffic,
                    "adopted_bootstrap": adopted_bootstrap,
                    "candidate_revision": candidate,
                    "runtime_service_account": (
                        f"tr-{surface}@quill-cloud-proxy.iam.gserviceaccount.com"
                    ),
                    "ingress": "internal-and-cloud-load-balancing",
                    "default_url_disabled": surface != "internal",
                    "concurrency": concurrency,
                    "min_instances": minimum,
                    "service_max_instances": maximum,
                    "revision_max_instances": maximum,
                    "timeout_seconds": timeout,
                    "memory": MEMORY[surface],
                    "cpu": 1,
                    "container_port": 8080,
                    "vpc_network": "default",
                    "vpc_subnet": "default",
                    "vpc_egress": "private-ranges-only",
                    "startup_probe_path": "/ready",
                    "startup_probe_initial_delay_seconds": 0,
                    "startup_probe_timeout_seconds": 10,
                    "startup_probe_period_seconds": 10,
                    "startup_probe_failure_threshold": 18,
                    "max_request_body_bytes": body,
                    "max_in_flight_request_body_bytes": inflight,
                    "max_concurrent_request_bodies": body_slots,
                    "request_body_read_timeout_seconds": read_timeout,
                    "postcondition_sha256": postcondition,
                }
            )
            live_services[f"{SERVICES[surface]}|{region}"] = service_json
            negs[f"{neg}|{region}"] = {"cloudRun": {"service": SERVICES[surface]}}

    legacy_backend = {
        "name": "trusted-router-control-backend",
        "selfLink": (
            "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
            "global/backendServices/trusted-router-control-backend"
        ),
        "loadBalancingScheme": "EXTERNAL_MANAGED",
        "protocol": "HTTP",
        "timeoutSec": 300,
        "backends": [
            {
                "group": (
                    "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                    f"regions/{region}/networkEndpointGroups/trusted-router-control-neg"
                )
            }
            for region in regions
        ],
    }
    backends["trusted-router-control-backend"] = legacy_backend
    legacy_backend_path = tmp_path / "legacy-backend.json"
    legacy_backend_path.write_text(json.dumps(legacy_backend), encoding="utf-8")
    legacy_backend_hash = _hash_json(legacy_backend_path, "hash-resource")
    legacy_hardening_regions: list[dict[str, str]] = []
    if mode == "initial_split":
        for region in regions:
            legacy_service, _, _ = _service_json("console", region)
            legacy_service = json.loads(
                json.dumps(legacy_service).replace(
                    "trusted-router-console", "trusted-router"
                )
            )
            legacy_service["status"]["latestCreatedRevisionName"] = (
                "trusted-router-prior"
            )
            legacy_service["status"]["latestReadyRevisionName"] = (
                "trusted-router-prior"
            )
            legacy_service["spec"]["template"]["spec"]["serviceAccountName"] = (
                "123456789-compute@developer.gserviceaccount.com"
            )
            legacy_path = tmp_path / f"legacy-service-{region}.json"
            legacy_path.write_text(json.dumps(legacy_service), encoding="utf-8")
            legacy_hash = _hash_json(legacy_path, "hash-service")
            legacy_revision = {
                "metadata": {"name": "trusted-router-prior"},
                "spec": legacy_service["spec"]["template"]["spec"],
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            }
            legacy_revision_path = tmp_path / f"legacy-revision-{region}.json"
            legacy_revision_path.write_text(
                json.dumps(legacy_revision), encoding="utf-8"
            )
            legacy_revision_hash = _hash_json(
                legacy_revision_path, "hash-revision"
            )
            legacy_iam = {
                "bindings": [
                    {"role": "roles/run.invoker", "members": ["allUsers"]}
                ]
            }
            legacy_iam_path = tmp_path / f"legacy-iam-{region}.json"
            legacy_iam_path.write_text(json.dumps(legacy_iam), encoding="utf-8")
            legacy_iam_hash = _hash_json(legacy_iam_path, "hash-iam-policy")
            hardening_revision_hash = hashlib.sha256(
                json.dumps(
                    {"annotations": {}, "spec": legacy_revision["spec"]},
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest()
            legacy_hardening_regions.append(
                {
                    "region": region,
                    "serving_revision": "trusted-router-prior",
                    "service_sha256": legacy_hash,
                    "revision_sha256": hardening_revision_hash,
                    "iam_sha256": legacy_iam_hash,
                }
            )
            live_services[f"trusted-router|{region}"] = legacy_service
            negs[f"trusted-router-control-neg|{region}"] = {
                "networkEndpointType": "SERVERLESS",
                "cloudRun": {"service": "trusted-router"},
            }
            legacy_fallback.append(
                {
                    "service": "trusted-router",
                    "backend": "trusted-router-control-backend",
                    "region": region,
                    "generation": 7,
                    "serving_revision": "trusted-router-prior",
                    "serving_revision_sha256": legacy_revision_hash,
                    "traffic": [
                        {
                            "revision": "trusted-router-prior",
                            "resolved_revision": "trusted-router-prior",
                            "latest_revision": False,
                            "percent": 100,
                            "tag": None,
                        }
                    ],
                    "postcondition_sha256": legacy_hash,
                    "backend_postcondition_sha256": legacy_backend_hash,
                    "invoker_iam_sha256": legacy_iam_hash,
                }
            )

    preserved = [
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
        *(f"api-{region}.quillrouter.com" for region in regions),
    ]
    manifest_path = tmp_path / "manifest.json"
    legacy_hardening_path = Path(f"{manifest_path}.legacy-hardening.json")
    legacy_hardening_sha256: str | None = None
    if mode == "initial_split":
        legacy_hardening_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "trusted-router-legacy-hardening-artifact",
                    "project_id": "quill-cloud-proxy",
                    "service": "trusted-router",
                    "runtime_service_account": (
                        "123456789-compute@developer.gserviceaccount.com"
                    ),
                    "operation_id": "legacy-hardener-test-operation",
                    "revision_suffix": "prior",
                    "regions": legacy_hardening_regions,
                    "secret_refs": [],
                    "journal_sha256": "9" * 64,
                    "created_at": "2026-08-20T11:00:00+00:00",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        legacy_hardening_path.chmod(0o600)
        legacy_hardening_sha256 = hashlib.sha256(
            legacy_hardening_path.read_bytes()
        ).hexdigest()
    manifest = {
        "schema_version": 1,
        "rollout_mode": mode,
        "project_id": "quill-cloud-proxy",
        "image": IMAGE,
        "release": "test-release",
        "created_at": "2026-08-20T12:00:00Z",
        "regions": regions,
        "primary_region": regions[0],
        "gateway_regions": regions,
        "internal_regions": internal_regions,
        "bootstrap_artifact_sha256": "2" * 64 if mode == "initial_split" else None,
        "legacy_hardening_artifact_sha256": legacy_hardening_sha256,
        "frontend_attestation_sha256": "0" * 64,
        "legacy_fallback": legacy_fallback,
        "domains": ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"],
        "preserved_hosts": preserved,
        "url_map": {
            "name": "trusted-router-map",
            "https_proxy": "trusted-router-control-https-proxy",
            "prior_snapshot": prior_path.name,
            "candidate_snapshot": candidate_path.name,
            "prior_sha256": prior_hash,
            "candidate_sha256": candidate_hash,
        },
        "promotion_state": "promotion-state.json",
        "services": services,
    }
    certificate_name = "trusted-router-test-cert"
    certificate_url = (
        "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
        f"global/sslCertificates/{certificate_name}"
    )
    cloud_state = {
        "url_map_name": "trusted-router-map",
        "https_proxy": "trusted-router-control-https-proxy",
        "live_map": "prior",
        "url_maps": {"prior": prior_map, "candidate": candidate_map, "unknown": {"name": "other"}},
        "other_url_maps": {},
        "services": live_services,
        "backends": backends,
        "policies": policies,
        "negs": negs,
        "iam": _iam_fixture(),
        "fail_before": [],
        "fail_after": [],
        "forwarding_rule": {
            "name": "trusted-router-https",
            "selfLink": (
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/forwardingRules/trusted-router-https"
            ),
            "IPAddress": FRONTEND_VIP,
            "IPProtocol": "TCP",
            "portRange": "443-443",
            "networkTier": "PREMIUM",
            "loadBalancingScheme": "EXTERNAL_MANAGED",
            "target": (
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/targetHttpsProxies/trusted-router-control-https-proxy"
            ),
        },
        "proxy": {
            "name": "trusted-router-control-https-proxy",
            "selfLink": (
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/targetHttpsProxies/trusted-router-control-https-proxy"
            ),
            "urlMap": prior_map["selfLink"],
            "sslCertificates": [certificate_url],
        },
        "ssl_certificates": {
            certificate_name: {
                "name": certificate_name,
                "selfLink": certificate_url,
                "type": "MANAGED",
                "managed": {"status": "ACTIVE", "domains": FRONTEND_HOSTS},
                "subjectAlternativeNames": FRONTEND_HOSTS,
            }
        },
        "dns": {
            host: {"A": [FRONTEND_VIP], "AAAA": []} for host in FRONTEND_HOSTS
        },
    }
    cloud_state_path = tmp_path / "cloud-state.json"
    cloud_state_path.write_text(json.dumps(cloud_state), encoding="utf-8")
    frontend_path = Path(f"{manifest_path}.frontend-attestation.json")
    _capture_frontend_artifact(tmp_path, cloud_state_path, frontend_path)
    manifest["frontend_attestation_sha256"] = hashlib.sha256(
        frontend_path.read_bytes()
    ).hexdigest()
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    manifest_path.chmod(0o600)
    _run_state("validate-manifest", manifest_path)
    return manifest_path, cloud_state_path, tmp_path / "events.log"


FAKE_GCLOUD = r'''#!/usr/bin/env python3
import json
import os
import sys
import time
from pathlib import Path

state_path = Path(os.environ["FAKE_GCLOUD_STATE"])
event_path = Path(os.environ["ROLLOUT_EVENT_LOG"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
if args[:1] == ["--project"]:
    args = args[2:]
joined = " ".join(args)
with event_path.open("a", encoding="utf-8") as output:
    output.write("gcloud " + joined + "\n")

def option(name):
    for index, arg in enumerate(args):
        if arg.startswith(name + "="):
            return arg.split("=", 1)[1]
        if arg == name and index + 1 < len(args):
            return args[index + 1]
    return None

def finish(value=None, code=0):
    state_path.write_text(json.dumps(state), encoding="utf-8")
    if value is not None:
        print(json.dumps(value) if not isinstance(value, str) else value)
    raise SystemExit(code)

def matching(bucket):
    for index, pattern in enumerate(state.get(bucket, [])):
        if pattern in joined:
            state[bucket].pop(index)
            state_path.write_text(json.dumps(state), encoding="utf-8")
            return True
    return False

if matching("fail_before"):
    finish(code=1)

for index, pattern in enumerate(state.get("sleep_before", [])):
    if pattern in joined:
        state["sleep_before"].pop(index)
        state_path.write_text(json.dumps(state), encoding="utf-8")
        time.sleep(float(state.get("sleep_seconds", 30)))
        break

if args[:2] == ["projects", "describe"]:
    finish("123456789")
if args[:2] == ["projects", "get-iam-policy"]:
    finish(state["iam"]["project"])
if args[:2] == ["projects", "get-ancestors"]:
    finish(state["iam"]["ancestors"])
if args[:3] == ["spanner", "instances", "list"]:
    finish([{"name": "projects/quill-cloud-proxy/instances/trusted-router-nam6"}])
if args[:3] == ["spanner", "databases", "list"]:
    finish([{"name": "projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router"}])
if args[:3] == ["spanner", "instances", "get-iam-policy"]:
    finish(state["iam"]["spanner_instance"])
if args[:3] == ["spanner", "databases", "get-iam-policy"]:
    finish(state["iam"]["spanner_database"])
if args[:3] == ["bigtable", "instances", "list"]:
    finish([{"name": "projects/quill-cloud-proxy/instances/trusted-router-logs"}])
if args[:3] == ["bigtable", "tables", "list"]:
    finish([{"name": "projects/quill-cloud-proxy/instances/trusted-router-logs/tables/trustedrouter-generations"}])
if args[:3] == ["bigtable", "instances", "get-iam-policy"]:
    finish(state["iam"]["bigtable_instance"])
if args[:3] == ["bigtable", "tables", "get-iam-policy"]:
    finish(state["iam"]["bigtable_table"])
if args[:3] == ["kms", "locations", "list"]:
    finish([{"locationId": "us-central1"}])
if args[:3] == ["kms", "keyrings", "list"]:
    finish([{"name": "projects/quill-cloud-proxy/locations/us-central1/keyRings/trusted-router"}])
if args[:3] == ["kms", "keys", "list"]:
    finish([
        {"name": "projects/quill-cloud-proxy/locations/us-central1/keyRings/trusted-router/cryptoKeys/byok-envelope"},
        {"name": "projects/quill-cloud-proxy/locations/us-central1/keyRings/trusted-router/cryptoKeys/google-ads-click-envelope"},
    ])
if args[:3] == ["kms", "keyrings", "get-iam-policy"]:
    finish(state["iam"]["kms_keyring"])
if args[:3] == ["kms", "keys", "get-iam-policy"]:
    key = args[3]
    finish(state["iam"]["byok" if key == "byok-envelope" else "google_ads"])
if args[:2] == ["secrets", "list"]:
    finish(state["iam"]["secrets"])
if args[:3] == ["secrets", "get-iam-policy"]:
    finish(state["iam"].get("secret_policies", {}).get(args[3], {"bindings": []}))
if args[:3] == ["storage", "buckets", "describe"]:
    finish(state["gcs"]["bucket"])
if args[:3] == ["storage", "buckets", "get-iam-policy"]:
    finish(state["gcs"]["policy"])
if args[:3] == ["storage", "objects", "describe"]:
    item = state["gcs"].get("objects", {}).get(args[3])
    if item is None:
        finish("NOT_FOUND", code=1)
    finish({"generation": str(item["generation"])})
if args[:2] == ["storage", "cp"]:
    source, destination = args[2:4]
    objects = state["gcs"].setdefault("objects", {})
    if source.startswith("gs://"):
        item = objects.get(source)
        if item is None:
            finish("NOT_FOUND", code=1)
        Path(destination).write_text(item["content"], encoding="utf-8")
        finish()
    expected = int(option("--if-generation-match"))
    current = objects.get(destination)
    actual = 0 if current is None else int(current["generation"])
    if state["gcs"].pop("conflict_once", False):
        if current is None:
            finish("precondition failed", code=1)
        current["generation"] = actual + 1
        objects[destination] = current
        finish("precondition failed", code=1)
    if actual != expected:
        finish("precondition failed", code=1)
    objects[destination] = {
        "generation": actual + 1,
        "content": Path(source).read_text(encoding="utf-8"),
    }
    finish(code=1 if matching("fail_after") else 0)
if args[:3] == ["compute", "forwarding-rules", "describe"]:
    finish(state["forwarding_rule"])
if args[:3] == ["compute", "target-https-proxies", "describe"]:
    if args[3] != state.get("https_proxy", "trusted-router-control-https-proxy"):
        finish("NOT_FOUND", code=1)
    if option("--format") == "json":
        finish(state["proxy"])
    finish(state.get("proxy_map", state.get("url_map_name", "trusted-router-map")))
if args[:3] == ["compute", "ssl-certificates", "describe"]:
    finish(state["ssl_certificates"][args[3]])
if args[:3] == ["iam", "roles", "describe"]:
    finish(state["gcs"]["role"])
if args[:3] == ["iam", "service-accounts", "describe"]:
    finish({"email": args[3], "disabled": False})
if args[:3] == ["iam", "service-accounts", "list"]:
    finish([
        {"email": f"tr-{surface}@quill-cloud-proxy.iam.gserviceaccount.com"}
        for surface in ("public", "actions", "console", "chat", "webhooks", "internal", "synthetic")
    ])
if args[:3] == ["iam", "service-accounts", "get-iam-policy"]:
    deploy = "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
    finish(
        {
            "bindings": [
                {"role": "roles/iam.serviceAccountUser", "members": [deploy]}
            ]
        }
    )
if args[:3] == ["compute", "url-maps", "list"]:
    names = [state.get("url_map_name", "trusted-router-map")]
    names.extend(sorted(state.get("other_url_maps", {})))
    finish("\n".join(names))
if args[:3] == ["compute", "url-maps", "describe"]:
    if args[3] in state.get("other_url_maps", {}):
        finish(state["other_url_maps"][args[3]])
    finish(state["url_maps"][state["live_map"]])
if args[:3] == ["compute", "url-maps", "import"]:
    source = option("--source") or ""
    state["live_map"] = "candidate" if "candidate" in source else "prior"
    finish(code=1 if matching("fail_after") else 0)
if args[:3] == ["run", "services", "describe"]:
    key = f"{args[3]}|{option('--region')}"
    finish(state["services"][key])
if args[:3] == ["run", "services", "list"]:
    inventory = []
    for key, service in state["services"].items():
        _, region = key.rsplit("|", 1)
        item = json.loads(json.dumps(service))
        item.setdefault("metadata", {}).setdefault("labels", {})[
            "cloud.googleapis.com/location"
        ] = region
        inventory.append(item)
    finish(inventory)
if args[:3] == ["run", "revisions", "describe"]:
    candidate = args[3]
    region = option("--region")
    for service in state["services"].values():
        if (
            service["status"]["latestReadyRevisionName"] == candidate
            and service["status"].get("url", "").endswith(f"-{region}.run.app")
        ) or (
            service["status"]["latestReadyRevisionName"] == candidate
            and any(key.endswith("|" + region) and value is service for key, value in state["services"].items())
        ):
            finish(
                {
                    "metadata": {"name": candidate},
                    "spec": service["spec"]["template"]["spec"],
                    "status": {
                        "conditions": [{"type": "Ready", "status": "True"}]
                    },
                }
            )
    finish("NOT_FOUND", code=1)
if args[:3] == ["run", "services", "get-iam-policy"]:
    key = f"{args[3]}|{option('--region')}"
    finish(
        state.get("service_iam", {}).get(
            key,
            {"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]},
        )
    )
if args[:3] == ["run", "services", "update-traffic"]:
    key = f"{args[3]}|{option('--region')}"
    service = state["services"][key]
    if option("--to-revisions") is not None:
        traffic = []
        for assignment in option("--to-revisions").split(","):
            revision, raw_percent = assignment.rsplit("=", 1)
            if revision == "LATEST":
                revision = service["status"]["latestReadyRevisionName"]
            traffic.append({"revisionName": revision, "percent": int(raw_percent)})
        tags = [item for item in service["spec"].get("traffic", []) if item.get("tag")]
        service["spec"]["traffic"] = traffic + tags
        service["status"]["traffic"] = traffic + tags
    if option("--set-tags") is not None:
        positive = [item for item in service["spec"].get("traffic", []) if not item.get("tag")]
        tags = []
        for assignment in option("--set-tags").split(","):
            tag, revision = assignment.split("=", 1)
            if revision == "LATEST":
                revision = service["status"]["latestReadyRevisionName"]
            tags.append({"revisionName": revision, "percent": 0, "tag": tag})
        service["spec"]["traffic"] = positive + tags
        service["status"]["traffic"] = positive + tags
    if "--clear-tags" in args:
        service["spec"]["traffic"] = [item for item in service["spec"].get("traffic", []) if not item.get("tag")]
        service["status"]["traffic"] = [item for item in service["status"].get("traffic", []) if not item.get("tag")]
    state["services"][key] = service
    finish(code=1 if matching("fail_after") else 0)
if args[:3] == ["compute", "backend-services", "describe"]:
    finish(state["backends"][args[3]])
if args[:3] == ["compute", "security-policies", "describe"]:
    finish(state["policies"][args[3]])
if args[:3] == ["compute", "network-endpoint-groups", "describe"]:
    finish(state["negs"][f"{args[3]}|{option('--region')}"])
finish(code=2)
'''

FAKE_DIG = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state = json.loads(Path(os.environ["FAKE_GCLOUD_STATE"]).read_text(encoding="utf-8"))
host, record_type = sys.argv[-2:]
for answer in state["dns"][host][record_type]:
    print(answer)
'''

FAKE_JQ = r'''#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
raw = any("r" in item for item in args if item.startswith("-") and not item.startswith("--"))
compact = any("c" in item for item in args if item.startswith("-") and not item.startswith("--"))
sort_keys = any("S" in item for item in args if item.startswith("-") and not item.startswith("--"))
exit_status = any("e" in item for item in args if item.startswith("-") and not item.startswith("--"))
variables = {}
position = 0
while position < len(args):
    if args[position] == "--arg":
        variables[args[position + 1]] = args[position + 2]
        position += 3
    elif args[position].startswith("-"):
        position += 1
    else:
        break
query = args[position]
files = args[position + 1:]
if files:
    data = json.loads(open(files[-1], encoding="utf-8").read())
else:
    data = json.load(sys.stdin)
normalized = " ".join(query.split())

def path_value(expression):
    value = data
    for part in expression.removeprefix(".").split("."):
        if part.endswith("[0]"):
            value = value[part[:-3]][0]
        else:
            value = value[part]
    return value

if normalized.startswith(".services[] | select(.surface == $surface)"):
    result = [item for item in data["services"] if item["surface"] == variables["surface"]]
elif normalized == '.services[] | select(.surface == "console")':
    result = [item for item in data["services"] if item["surface"] == "console"]
elif normalized == '[.services[] | select(.surface == $surface)][0]':
    result = next(item for item in data["services"] if item["surface"] == variables["surface"])
elif normalized == '.services[]':
    result = data["services"]
elif normalized == '.legacy_fallback[]':
    result = data["legacy_fallback"]
elif normalized == '.regions[]':
    result = data["regions"]
elif normalized == '.legacy_hardening_artifact_sha256 // ""':
    result = data.get("legacy_hardening_artifact_sha256") or ""
elif normalized == '.cloudRun.service // ""':
    result = (data.get("cloudRun") or {}).get("service", "")
elif normalized.startswith(".email == $email and"):
    result = data.get("email") == variables["email"] and not data.get("disabled", False)
elif "roles/iam.serviceAccountUser" in normalized and "$member" in normalized:
    direct = [
        {
            "role": binding.get("role"),
            "condition": binding.get("condition"),
        }
        for binding in data.get("bindings", [])
        if variables["member"] in binding.get("members", [])
    ]
    result = direct == [
        {"role": "roles/iam.serviceAccountUser", "condition": None}
    ]
elif normalized == 'any(.pathMatchers[]?; .name == "trusted-router-service-surfaces")':
    result = any(
        item.get("name") == "trusted-router-service-surfaces"
        for item in data.get("pathMatchers", [])
    )
elif normalized == "any(.[]; .latest_revision)":
    result = any(item.get("latest_revision") for item in data)
elif normalized.startswith("if .status.latestReadyRevisionName == $revision"):
    result = (
        data.get("status", {}).get("latestReadyRevisionName") == variables["revision"]
        and any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in data.get("status", {}).get("conditions", [])
        )
    )
elif normalized.startswith("if (.metadata.generation | type) == \"number\""):
    result = data["metadata"]["generation"]
    if result != data["status"]["observedGeneration"]:
        raise SystemExit(1)
elif normalized.startswith("all(.legacy_fallback[];"):
    result = all(
        item["backend"] == variables["backend"]
        and item["backend_postcondition_sha256"] == variables["digest"]
        for item in data["legacy_fallback"]
    )
elif 'select(any(.members[]?; . == "allUsers"))' in normalized:
    matches = [
        {
            "role": binding.get("role"),
            "condition": binding.get("condition"),
            "allUsersCount": binding.get("members", []).count("allUsers"),
        }
        for binding in data.get("bindings", [])
        if "allUsers" in binding.get("members", [])
    ]
    result = matches == [
        {
            "role": "roles/run.invoker",
            "condition": None,
            "allUsersCount": 1,
        }
    ]
elif "select(.revisionName == $revision)" in normalized and "add // 0" in normalized:
    result = sum(
        int(item.get("percent", 0) or 0)
        for item in data.get("status", {}).get("traffic", [])
        if item.get("revisionName") == variables["revision"]
    )
elif normalized.startswith('[.prior_traffic[] | select(.tag != null'):
    result = ",".join(
        f"{item['tag']}={'LATEST' if item['latest_revision'] else item['revision']}"
        for item in data["prior_traffic"]
        if item.get("tag")
    )
elif normalized.startswith("sort_by(.tag"):
    result = sorted(
        data,
        key=lambda item: (
            item.get("tag") or "",
            item.get("latest_revision"),
            item.get("revision") or "",
            item.get("percent"),
        ),
    )
elif normalized.startswith(".prior_traffic | sort_by(.tag"):
    result = sorted(
        data["prior_traffic"],
        key=lambda item: (
            item.get("tag") or "",
            item.get("latest_revision"),
            item.get("revision") or "",
            item.get("percent"),
        ),
    )
elif normalized.startswith(".schema_version == 1 and .manifest_sha256"):
    result = (
        data.get("schema_version") == 1
        and data.get("manifest_sha256") == variables["manifest"]
        and data.get("project_id") == variables["project"]
        and data.get("url_map_name") == variables["map"]
        and data.get("prior_url_map_sha256") == variables["prior"]
        and data.get("candidate_url_map_sha256") == variables["candidate"]
        and isinstance(data.get("attempts"), list)
    )
elif '.operation == "traffic"' in normalized:
    result = any(
        item.get("operation") == "traffic"
        and item.get("service") == variables["service"]
        and item.get("region") == variables["region"]
        for item in data.get("attempts", [])
    )
elif '.operation == "url-map"' in normalized:
    result = any(item.get("operation") == "url-map" for item in data.get("attempts", []))
elif normalized.startswith(".") and all(token not in normalized for token in " |(){}"):
    result = path_value(normalized)
else:
    raise SystemExit(f"unsupported fake jq query: {normalized}")

truthy = result is not False and result is not None
if exit_status and not truthy:
    raise SystemExit(1)
if isinstance(result, list) and normalized in {
    ".services[]",
    ".legacy_fallback[]",
    ".regions[]",
} or normalized.startswith(".services[] |"):
    for item in result:
        print(
            item
            if raw and isinstance(item, str)
            else json.dumps(item, separators=(",", ":"), sort_keys=sort_keys)
        )
elif raw and isinstance(result, bool):
    print("true" if result else "false")
elif raw and isinstance(result, (str, int, float)):
    print(result)
else:
    print(
        json.dumps(
            result,
            separators=(",", ":") if compact else None,
            sort_keys=sort_keys,
        )
    )
'''


def _install_fake_gcloud(tmp_path: Path) -> Path:
    binary = tmp_path / "bin" / "gcloud"
    binary.parent.mkdir(exist_ok=True)
    binary.write_text(FAKE_GCLOUD, encoding="utf-8")
    binary.chmod(0o755)
    jq = binary.parent / "jq"
    jq.write_text(FAKE_JQ, encoding="utf-8")
    jq.chmod(0o755)
    dig = binary.parent / "dig"
    dig.write_text(FAKE_DIG, encoding="utf-8")
    dig.chmod(0o755)
    curl = binary.parent / "curl"
    curl.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "args=sys.argv[1:]\n"
        "output=args[args.index('--output')+1]\n"
        "url=next(item for item in args if item.startswith('https://'))\n"
        "if url.endswith('/chat-proxy/v1/chat/completions') or url.endswith('/v1/internal/gateway/authorize'):\n"
        " message='Missing Authentication header' if '/chat-proxy/' in url else 'Invalid internal service token'\n"
        " request_id=next(value for i,value in enumerate(args) if i > 0 and args[i-1] == '--header' and value.startswith('x-request-id: ')).split(': ',1)[1]\n"
        " body={'error': {'code': 401, 'message': message, 'type': 'unauthorized', 'source': 'router'}}\n"
        " headers=args[args.index('--dump-header')+1]\n"
        " Path(headers).write_text('HTTP/2 401\\ncontent-type: application/json\\nx-trustedrouter-request-id: '+request_id+'\\nstrict-transport-security: max-age=63072000; includeSubDomains\\n\\n', encoding='iso-8859-1')\n"
        " code='401'\n"
        "elif url.endswith('/auth/session'):\n"
        " body={'data': {'authenticated': True, 'management': True}}; code='200'\n"
        "else:\n"
        " body={}; code='422' if url.endswith('/support/inquiry') else '400' if url.endswith('/internal/stripe/webhook') else '200'\n"
        "Path(output).write_text(json.dumps(body), encoding='utf-8')\n"
        "print(code, end='')\n",
        encoding="utf-8",
    )
    curl.chmod(0o755)
    npx = binary.parent / "npx"
    npx.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "phase=os.environ.get('TR_ROLLOUT_SMOKE_PHASE','')\n"
        "percent=os.environ.get('TR_ROLLOUT_SMOKE_PERCENT','')\n"
        "with Path(os.environ['ROLLOUT_EVENT_LOG']).open('a', encoding='utf-8') as out: out.write(f'smoke {phase} {percent}\\n')\n"
        "if os.environ.get('OOB_IMPORT_PHASE') == phase:\n"
        " p=Path(os.environ['FAKE_GCLOUD_STATE']); s=json.loads(p.read_text()); s['live_map']='candidate'; p.write_text(json.dumps(s))\n"
        "raise SystemExit(1 if os.environ.get('FAIL_SMOKE_PHASE') == phase else 0)\n",
        encoding="utf-8",
    )
    npx.chmod(0o755)
    return binary.parent


def _capture_frontend_artifact(
    tmp_path: Path, cloud_state: Path, artifact: Path
) -> None:
    fake_bin = _install_fake_gcloud(tmp_path)
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_GCLOUD_STATE": str(cloud_state),
        "ROLLOUT_EVENT_LOG": str(tmp_path / "frontend-events.log"),
    }
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(FRONTEND_TOOL),
            "capture",
            "--project",
            PROJECT,
            "--forwarding-rule",
            "trusted-router-https",
            "--https-proxy",
            "trusted-router-control-https-proxy",
            "--url-map",
            "trusted-router-map",
            "--hosts",
            ",".join(FRONTEND_HOSTS),
            "--artifact",
            str(artifact),
        ],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def _smoke_callback(tmp_path: Path) -> Path:
    callback = tmp_path / "smoke"
    callback.write_text(
        "#!/usr/bin/env bash\n"
        "echo \"smoke $2 $3\" >> \"$ROLLOUT_EVENT_LOG\"\n"
        "if [ \"${OOB_IMPORT_PHASE:-}\" = \"$2\" ]; then\n"
        "  python3 - \"$FAKE_GCLOUD_STATE\" <<'PY'\n"
        "import json, sys\n"
        "from pathlib import Path\n"
        "path = Path(sys.argv[1])\n"
        "state = json.loads(path.read_text(encoding='utf-8'))\n"
        "state['live_map'] = 'candidate'\n"
        "path.write_text(json.dumps(state), encoding='utf-8')\n"
        "PY\n"
        "fi\n"
        "[ \"${FAIL_SMOKE_PHASE:-}\" != \"$2\" ]\n",
        encoding="utf-8",
    )
    callback.chmod(0o755)
    return callback


def _run_helper(
    tmp_path: Path,
    manifest: Path,
    cloud_state: Path,
    events: Path,
    *arguments: str,
    fail_smoke: str = "",
    invalid_smoke: bool = False,
    durable: bool = False,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    fake_bin = _install_fake_gcloud(tmp_path)
    manifest_value = json.loads(manifest.read_text(encoding="utf-8"))
    auth_header = tmp_path / "smoke-authorization.header"
    storage_state = tmp_path / "smoke-storage-state.json"
    auth_header.write_text(
        "Authorization: Bearer test-rollout-token-1234567890\n", encoding="utf-8"
    )
    storage_state.write_text('{"cookies":[],"origins":[]}\n', encoding="utf-8")
    auth_header.chmod(0o600)
    storage_state.chmod(0o600)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_GCLOUD_STATE": str(cloud_state),
        "ROLLOUT_EVENT_LOG": str(events),
        "TR_ROLLOUT_SMOKE_COMMAND": str(
            tmp_path / "missing-smoke"
            if invalid_smoke
            else SMOKE
        ),
        "TR_ROLLOUT_SMOKE_AUTH_HEADER_FILE": str(auth_header),
        "TR_ROLLOUT_SMOKE_PLAYWRIGHT_STORAGE_STATE": str(storage_state),
        "TR_ROLLOUT_SMOKE_PRODUCTION_APPROVED": "true",
        "TR_CONTROL_PLANE_REGIONS": ",".join(manifest_value["regions"]),
        "TR_REGIONS": ",".join(manifest_value["gateway_regions"]),
        "FAIL_SMOKE_PHASE": fail_smoke,
        "TR_ROLLOUT_REQUIRE_DURABLE_STATE": "false",
    }
    if durable:
        env.update(
            {
                "TR_ROLLOUT_REQUIRE_DURABLE_STATE": "true",
                "TR_ROLLOUT_STATE_GCS_URI": STATE_GCS_URI,
                "TR_ROLLOUT_STATE_GCS_ROLE": STATE_GCS_ROLE,
                "TR_ROLLOUT_OPERATION_ID": "test-operation-0001",
            }
        )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(HELPER), arguments[0], str(manifest), *arguments[1:]],
        env=env,
        capture_output=True,
        text=True,
    )


def _run_recovery(
    tmp_path: Path,
    cloud_state: Path,
    *arguments: str | Path,
    bundle_uri: str = RECOVERY_BUNDLE,
) -> subprocess.CompletedProcess[str]:
    fake_bin = _install_fake_gcloud(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_GCLOUD_STATE": str(cloud_state),
        "ROLLOUT_EVENT_LOG": str(tmp_path / "recovery-events.log"),
        "TR_ROLLOUT_RECOVERY_GCS_PREFIX": RECOVERY_PREFIX,
        "TR_ROLLOUT_BUNDLE_GCS_URI": bundle_uri,
        "TR_ROLLOUT_AUTHORITY_GCS_URI": RECOVERY_AUTHORITY,
    }
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(RECOVERY), *(str(value) for value in arguments)],
        env=env,
        capture_output=True,
        text=True,
    )


def _promotion_state_value(manifest_path: Path, attempts: list[dict[str, Any]]) -> dict[str, Any]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {
        "schema_version": 1,
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "project_id": manifest["project_id"],
        "url_map_name": manifest["url_map"]["name"],
        "prior_url_map_sha256": manifest["url_map"]["prior_sha256"],
        "candidate_url_map_sha256": manifest["url_map"]["candidate_sha256"],
        "attempts": attempts,
    }


def _configure_durable_state(state_path: Path, manifest_path: Path) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["gcs"] = {
        "bucket": {
            "iamConfiguration": {
                "uniformBucketLevelAccess": {"enabled": True},
                "publicAccessPrevention": "enforced",
            },
            "versioning": {"enabled": True},
            "retentionPolicy": {"retentionPeriod": 604800},
        },
        "policy": {
            "bindings": [
                {
                    "role": STATE_GCS_ROLE,
                    "members": [
                        "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
                    ],
                    "condition": {
                        "title": "trusted-router-rollout-journal",
                        "expression": (
                            'resource.name == "projects/_/buckets/'
                            'trusted-router-rollout-state/objects/releases/test/'
                            'promotion-state.json"'
                        ),
                    },
                }
            ]
        },
        "role": {
            "name": STATE_GCS_ROLE,
            "stage": "GA",
            "includedPermissions": [
                "storage.objects.create",
                "storage.objects.delete",
                "storage.objects.get",
            ],
        },
        "objects": {},
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _put_durable_journal(
    state_path: Path,
    manifest_path: Path,
    attempts: list[dict[str, Any]],
    *,
    generation: int = 1,
) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["gcs"]["objects"][STATE_GCS_URI] = {
        "generation": generation,
        "content": json.dumps(_promotion_state_value(manifest_path, attempts), indent=2, sort_keys=True)
        + "\n",
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _candidate_percents(state_path: Path) -> set[int]:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    values = set()
    for service in state["services"].values():
        candidate = service["status"]["latestReadyRevisionName"]
        values.add(
            sum(
                int(item.get("percent", 0))
                for item in service["status"]["traffic"]
                if item.get("revisionName") == candidate
            )
        )
    return values


def _set_candidate_percent(state_path: Path, percent: int) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for service in state["services"].values():
        candidate = service["status"]["latestReadyRevisionName"]
        prior = service["spec"]["traffic"][0]["revisionName"]
        traffic = [{"revisionName": candidate, "percent": percent}]
        if percent < 100:
            traffic.append({"revisionName": prior, "percent": 100 - percent})
        service["spec"]["traffic"] = copy.deepcopy(traffic)
        service["status"]["traffic"] = copy.deepcopy(traffic)
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _set_service_candidate_percent(
    state_path: Path,
    service_name: str,
    region: str,
    percent: int,
) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    service = state["services"][f"{service_name}|{region}"]
    candidate = service["status"]["latestReadyRevisionName"]
    prior = service["spec"]["traffic"][0]["revisionName"]
    traffic = [{"revisionName": candidate, "percent": percent}]
    if percent < 100:
        traffic.append({"revisionName": prior, "percent": 100 - percent})
    service["spec"]["traffic"] = copy.deepcopy(traffic)
    service["status"]["traffic"] = copy.deepcopy(traffic)
    state_path.write_text(json.dumps(state), encoding="utf-8")


def _traffic_state_attempts(
    manifest_path: Path,
    percent: int,
    *,
    regions: set[str] | None = None,
) -> list[dict[str, Any]]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [
        {
            "attempted_at": "2026-08-20T12:00:00+00:00",
            "operation": "traffic-state",
            "surface": entry["surface"],
            "service": entry["name"],
            "region": entry["region"],
            "target": str(percent),
        }
        for entry in manifest["services"]
        if regions is None or entry["region"] in regions
    ]


def _write_local_promotion_state(
    manifest_path: Path,
    attempts: list[dict[str, Any]],
) -> Path:
    path = manifest_path.parent / "promotion-state.json"
    path.write_text(
        json.dumps(_promotion_state_value(manifest_path, attempts), indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


def _stage_fixture(tmp_path: Path, *, floating_latest: bool) -> tuple[Path, Path]:
    region = "us-central1"
    services: dict[str, Any] = {}
    for surface in SURFACES:
        service, _, prior = _service_json(surface, region)
        if floating_latest and surface == "public":
            service["spec"]["traffic"] = [
                {"latestRevision": True, "percent": 100}
            ]
            service["status"]["traffic"] = [
                {"revisionName": prior, "percent": 100}
            ]
        services[f"{SERVICES[surface]}|{region}"] = service
    url_map = {
        "name": "trusted-router-map",
        "selfLink": (
            "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
            "global/urlMaps/trusted-router-map"
        ),
        "defaultService": (
            "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
            "global/backendServices/legacy-backend"
        ),
        "hostRules": [
            {
                "hosts": [
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
                    "api-us-central1.quillrouter.com",
                ],
                "pathMatcher": "preserved-apis",
            }
        ],
        "pathMatchers": [
            {
                "name": "preserved-apis",
                "defaultService": (
                    "https://www.googleapis.com/compute/v1/projects/"
                    "quill-cloud-proxy/global/backendServices/legacy-backend"
                ),
            },
            {
                # These fixtures pre-create all six split services and are
                # meant to exercise the existing-split traffic validator. The
                # reserved matcher is the rollout's authoritative mode marker;
                # without it the stricter initial-split inventory correctly
                # rejects the pre-existing public companion before the
                # intended traffic-shape assertion is reached.
                "name": "trusted-router-service-surfaces",
                "defaultService": (
                    "https://www.googleapis.com/compute/v1/projects/"
                    "quill-cloud-proxy/global/backendServices/"
                    "trusted-router-public-backend"
                ),
            },
        ],
    }
    certificate_name = "trusted-router-stage-cert"
    certificate_url = (
        "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
        f"global/sslCertificates/{certificate_name}"
    )
    state = {
        "url_map_name": "trusted-router-map",
        "https_proxy": "trusted-router-control-https-proxy",
        "live_map": "prior",
        "url_maps": {"prior": url_map, "candidate": url_map},
        "other_url_maps": {},
        "services": services,
        "backends": {},
        "policies": {},
        "negs": {},
        "iam": _iam_fixture(),
        "fail_before": [],
        "fail_after": [],
        "forwarding_rule": {
            "name": "trusted-router-https",
            "selfLink": (
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/forwardingRules/trusted-router-https"
            ),
            "IPAddress": FRONTEND_VIP,
            "IPProtocol": "TCP",
            "portRange": "443-443",
            "networkTier": "PREMIUM",
            "loadBalancingScheme": "EXTERNAL_MANAGED",
            "target": (
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/targetHttpsProxies/trusted-router-control-https-proxy"
            ),
        },
        "proxy": {
            "name": "trusted-router-control-https-proxy",
            "selfLink": (
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/targetHttpsProxies/trusted-router-control-https-proxy"
            ),
            "urlMap": url_map["selfLink"],
            "sslCertificates": [certificate_url],
        },
        "ssl_certificates": {
            certificate_name: {
                "name": certificate_name,
                "selfLink": certificate_url,
                "type": "MANAGED",
                "managed": {"status": "ACTIVE", "domains": FRONTEND_HOSTS},
                "subjectAlternativeNames": FRONTEND_HOSTS,
            }
        },
        "dns": {
            host: {"A": [FRONTEND_VIP], "AAAA": []} for host in FRONTEND_HOSTS
        },
    }
    state_path = tmp_path / "stage-cloud-state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    return state_path, tmp_path / "stage-events.log"


def _run_stage(
    tmp_path: Path,
    state: Path,
    events: Path,
    *,
    revision_suffix: str = "rtest1",
    regions: str = "us-central1",
) -> subprocess.CompletedProcess[str]:
    fake_bin = _install_fake_gcloud(tmp_path)
    frontend_attestation = tmp_path / "stage-frontend-attestation.json"
    _capture_frontend_artifact(tmp_path, state, frontend_attestation)
    env = {
        **os.environ,
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_GCLOUD_STATE": str(state),
        "ROLLOUT_EVENT_LOG": str(events),
        "TR_CONTROL_PLANE_REGIONS": regions,
        "TR_REGIONS": regions,
        "TR_PRIMARY_REGION": regions.split(",")[0],
        "TR_SYNTHETIC_MONITOR_REGIONS": regions,
        "TR_SYNTHETIC_THROUGHPUT_REGION": regions.split(",")[0],
        "TR_SYNTHETIC_IMAGE_REGION": regions.split(",")[0],
        "TR_SYNTHETIC_VIDEO_REGION": regions.split(",")[0],
        "TR_RELEASE": "test-release",
        "TR_ROLLOUT_REVISION_SUFFIX": revision_suffix,
        "TR_ROLLOUT_OPERATION_ID": "stage-fixture-operation",
        "TR_ROLLOUT_LOCAL_LOCK_PATH": str(tmp_path / "rollout-operation.lock"),
        "TR_ROLLOUT_FRONTEND_ATTESTATION": str(frontend_attestation),
        "IMAGE": IMAGE,
    }
    return subprocess.run(  # noqa: S603
        ["/bin/bash", str(ROLLOUT), "--manifest", str(tmp_path / "stage-manifest.json")],
        env=env,
        capture_output=True,
        text=True,
    )


def _mutation_events(events: Path) -> list[str]:
    if not events.exists():
        return []
    mutation_markers = (
        " run deploy ",
        " services update ",
        " url-maps import ",
        " backend-services create ",
        " backend-services update ",
        " backend-services add-backend ",
        " backend-services remove-backend ",
        " network-endpoint-groups create ",
        " security-policies create ",
        " security-policies rules create ",
        " security-policies rules update ",
    )
    return [
        line
        for line in events.read_text(encoding="utf-8").splitlines()
        if any(marker in f" {line} " for marker in mutation_markers)
    ]


def test_stage_rejects_floating_latest_before_any_mutation(tmp_path: Path) -> None:
    state, events = _stage_fixture(tmp_path, floating_latest=True)
    result = _run_stage(tmp_path, state, events)
    assert result.returncode != 0
    assert "floating LATEST traffic or tags" in result.stderr
    assert _mutation_events(events) == []
    assert not (tmp_path / "stage-manifest.json").exists()


def test_stage_rejects_positive_tagged_prior_traffic_before_any_mutation(
    tmp_path: Path,
) -> None:
    state_path, events = _stage_fixture(tmp_path, floating_latest=False)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    service = state["services"]["trusted-router-public|us-central1"]
    prior = service["spec"]["traffic"][0]["revisionName"]
    tagged = [{"revisionName": prior, "percent": 100, "tag": "stable"}]
    service["spec"]["traffic"] = copy.deepcopy(tagged)
    service["status"]["traffic"] = copy.deepcopy(tagged)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_stage(tmp_path, state_path, events)

    assert result.returncode != 0
    assert "positive traffic targets must be untagged" in result.stderr
    assert _mutation_events(events) == []
    assert not (tmp_path / "stage-manifest.json").exists()


def test_stage_rejects_untagged_zero_percent_prior_before_any_mutation(
    tmp_path: Path,
) -> None:
    state_path, events = _stage_fixture(tmp_path, floating_latest=False)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    service = state["services"]["trusted-router-public|us-central1"]
    prior = service["spec"]["traffic"][0]["revisionName"]
    traffic = [
        {"revisionName": prior, "percent": 100},
        {"revisionName": "trusted-router-public-retired", "percent": 0},
    ]
    service["spec"]["traffic"] = copy.deepcopy(traffic)
    service["status"]["traffic"] = copy.deepcopy(traffic)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_stage(tmp_path, state_path, events)

    assert result.returncode != 0
    assert "untagged zero-percent traffic targets" in result.stderr
    assert _mutation_events(events) == []
    assert not (tmp_path / "stage-manifest.json").exists()


def test_stage_refuses_existing_recovery_snapshot_before_any_mutation(tmp_path: Path) -> None:
    state, events = _stage_fixture(tmp_path, floating_latest=False)
    (tmp_path / "url-map.prior.json").write_text("{}", encoding="utf-8")
    result = _run_stage(tmp_path, state, events)
    assert result.returncode != 0
    assert "refusing to overwrite existing rollout recovery artifact" in result.stderr
    assert _mutation_events(events) == []


@pytest.mark.parametrize(
    ("revision_suffix", "regions", "message"),
    [
        ("r_bad", "us-central1", "canonical revision suffix"),
        ("rtest1", "us-central1,us-central1", "duplicate control-plane region"),
    ],
)
def test_stage_rejects_malformed_identifiers_before_any_mutation(
    tmp_path: Path,
    revision_suffix: str,
    regions: str,
    message: str,
) -> None:
    state, events = _stage_fixture(tmp_path, floating_latest=False)
    result = _run_stage(
        tmp_path,
        state,
        events,
        revision_suffix=revision_suffix,
        regions=regions,
    )
    assert result.returncode != 0
    assert message in result.stderr
    assert _mutation_events(events) == []


def test_existing_split_promotes_every_surface_in_primary_then_secondary(tmp_path: Path) -> None:
    manifest, state, events = _fixture(tmp_path)
    result = _run_helper(tmp_path, manifest, state, events, "promote")
    assert result.returncode == 0, result.stderr
    assert _candidate_percents(state) == {100}
    lines = events.read_text(encoding="utf-8").splitlines()
    smokes = [line for line in lines if line.startswith("smoke ")]
    assert smokes == [
        "smoke preflight 0",
        "smoke primary 10",
        "smoke primary 50",
        "smoke primary 100",
        "smoke secondary 10",
        "smoke secondary 50",
        "smoke secondary 100",
    ]
    first_secondary = next(index for index, line in enumerate(lines) if "--region=europe-west4" in line and "update-traffic" in line)
    primary_100_smoke = lines.index("smoke primary 100")
    assert primary_100_smoke < first_secondary
    assert not any("url-maps import" in line for line in lines)


def test_private_internal_synthetic_region_is_verified_but_not_added_to_lb(
    tmp_path: Path,
) -> None:
    manifest, state, events = _fixture(
        tmp_path,
        regions=["us-central1"],
        internal_regions=["us-central1", "europe-west4"],
    )

    result = _run_helper(tmp_path, manifest, state, events, "verify", "all", "0")

    assert result.returncode == 0, result.stderr
    cloud = json.loads(state.read_text(encoding="utf-8"))
    internal_backend = cloud["backends"]["trusted-router-billing-backend"]
    assert len(internal_backend["backends"]) == 1
    assert "us-central1" in internal_backend["backends"][0]["group"]
    assert "europe-west4" not in internal_backend["backends"][0]["group"]


def test_promote_requires_executable_smoke_before_any_mutation(tmp_path: Path) -> None:
    manifest, state, events = _fixture(tmp_path, regions=["us-central1"])
    result = _run_helper(
        tmp_path,
        manifest,
        state,
        events,
        "promote",
        invalid_smoke=True,
    )
    assert result.returncode != 0
    assert "must be an executable file" in result.stderr
    assert _mutation_events(events) == []


def test_preflight_dns_tls_smoke_failure_does_zero_mutations(tmp_path: Path) -> None:
    manifest, state, events = _fixture(tmp_path, regions=["us-central1"])
    result = _run_helper(
        tmp_path,
        manifest,
        state,
        events,
        "promote",
        fail_smoke="preflight",
    )
    assert result.returncode != 0
    lines = events.read_text(encoding="utf-8").splitlines()
    assert "smoke preflight 0" in lines
    assert not any("update-traffic" in line or "url-maps import" in line for line in lines)


def test_required_durable_journal_configuration_fails_before_mutation(
    tmp_path: Path,
) -> None:
    manifest, state, events = _fixture(tmp_path, regions=["us-central1"])
    result = _run_helper(
        tmp_path,
        manifest,
        state,
        events,
        "promote-step",
        "primary",
        "10",
        extra_env={"TR_ROLLOUT_REQUIRE_DURABLE_STATE": "true"},
    )
    assert result.returncode != 0
    assert "requires TR_ROLLOUT_STATE_GCS_URI" in result.stderr
    assert _mutation_events(events) == []


def test_private_recovery_bundle_restores_a_fresh_runner(tmp_path: Path) -> None:
    manifest, state_path, _ = _fixture(tmp_path, regions=["us-central1"])
    _configure_durable_state(state_path, manifest)

    published = _run_recovery(tmp_path, state_path, "publish", manifest)
    assert published.returncode == 0, published.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    objects = state["gcs"]["objects"]
    expected = {
        f"{RECOVERY_BUNDLE}/bundle.json",
        f"{RECOVERY_BUNDLE}/{manifest.name}",
        f"{RECOVERY_BUNDLE}/url-map.prior.json",
        f"{RECOVERY_BUNDLE}/url-map.candidate.json",
        f"{RECOVERY_BUNDLE}/promotion-state.json",
        RECOVERY_AUTHORITY,
    }
    assert expected <= set(objects)

    destination = tmp_path / "fresh-runner"
    recovered = _run_recovery(
        tmp_path,
        state_path,
        "recover",
        RECOVERY_BUNDLE,
        destination,
    )
    assert recovered.returncode == 0, recovered.stderr
    recovered_manifest = Path(recovered.stdout.strip())
    assert recovered_manifest == destination / manifest.name
    assert all(
        path.stat().st_mode & 0o777 == 0o600
        for path in destination.iterdir()
        if path.is_file()
    )
    _run_state("validate-manifest", recovered_manifest)


def test_publish_refuses_to_supersede_another_active_manifest(
    tmp_path: Path,
) -> None:
    first_dir = tmp_path / "first"
    first_dir.mkdir()
    first, state_path, _ = _fixture(first_dir, regions=["us-central1"])
    _configure_durable_state(state_path, first)
    published = _run_recovery(first_dir, state_path, "publish", first)
    assert published.returncode == 0, published.stderr
    before = json.loads(state_path.read_text(encoding="utf-8"))["gcs"]["objects"][
        RECOVERY_AUTHORITY
    ]["content"]

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second, _, _ = _fixture(second_dir, regions=["us-central1"])
    second_value = json.loads(second.read_text(encoding="utf-8"))
    second_value["release"] = "different-release"
    second.write_text(json.dumps(second_value), encoding="utf-8")
    refused = _run_recovery(
        second_dir,
        state_path,
        "publish",
        second,
        bundle_uri=f"{RECOVERY_PREFIX}/releases/test-epoch-0002",
    )
    assert refused.returncode != 0
    assert "refusing to overwrite" in refused.stderr
    after = json.loads(state_path.read_text(encoding="utf-8"))["gcs"]["objects"][
        RECOVERY_AUTHORITY
    ]["content"]
    assert after == before


def test_durable_journal_resumes_on_a_fresh_runner(tmp_path: Path) -> None:
    manifest, state, events = _fixture(tmp_path, regions=["us-central1"])
    _configure_durable_state(state, manifest)
    first = _run_helper(
        tmp_path,
        manifest,
        state,
        events,
        "promote-step",
        "primary",
        "10",
        durable=True,
    )
    assert first.returncode == 0, first.stderr
    local_journal = tmp_path / "promotion-state.json"
    assert local_journal.stat().st_mode & 0o777 == 0o600
    local_journal.unlink()

    resumed = _run_helper(
        tmp_path,
        manifest,
        state,
        events,
        "promote-step",
        "primary",
        "50",
        durable=True,
    )

    assert resumed.returncode == 0, resumed.stderr
    assert _candidate_percents(state) == {50}
    assert local_journal.exists()
    assert local_journal.stat().st_mode & 0o777 == 0o600
    lines = events.read_text(encoding="utf-8").splitlines()
    assert any("storage cp" in line and "--if-generation-match=" in line for line in lines)


def test_durable_journal_cas_conflict_stops_before_provider_mutation(
    tmp_path: Path,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    _configure_durable_state(state_path, manifest)
    _put_durable_journal(state_path, manifest, [])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["gcs"]["conflict_once"] = True
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_helper(
        tmp_path,
        manifest,
        state_path,
        events,
        "promote-step",
        "primary",
        "10",
        durable=True,
    )

    assert result.returncode != 0
    assert _candidate_percents(state_path) == {0}
    assert not any(
        "run services update-traffic" in line
        for line in events.read_text(encoding="utf-8").splitlines()
    )


def test_active_operation_lease_rejects_concurrent_rollback_without_mutation(
    tmp_path: Path,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    promotion = _promotion_state_value(manifest, [])
    promotion["lease"] = {
        "owner": "first-operation-0001",
        "operation": "promote-step:primary:10",
        "acquired_at": "2026-08-21T00:00:00+00:00",
        "expires_at": "2099-08-21T00:00:00+00:00",
    }
    promotion_path = tmp_path / "promotion-state.json"
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")
    promotion_path.chmod(0o600)

    refused = _run_helper(
        tmp_path,
        manifest,
        state_path,
        events,
        "rollback",
        extra_env={"TR_ROLLOUT_OPERATION_ID": "second-operation-0002"},
    )
    assert refused.returncode != 0
    assert "active lease" in refused.stderr
    assert _mutation_events(events) == []
    assert json.loads(promotion_path.read_text(encoding="utf-8"))["lease"][
        "owner"
    ] == "first-operation-0001"


def test_expired_operation_lease_requires_explicit_reconciled_takeover(
    tmp_path: Path,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    promotion = _promotion_state_value(manifest, [])
    promotion["lease"] = {
        "owner": "crashed-operation-0001",
        "operation": "promote-step:primary:10",
        "acquired_at": "2000-01-01T00:00:00+00:00",
        "expires_at": "2000-01-01T00:01:00+00:00",
    }
    promotion_path = tmp_path / "promotion-state.json"
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")
    promotion_path.chmod(0o600)

    refused = _run_helper(
        tmp_path,
        manifest,
        state_path,
        events,
        "rollback",
        extra_env={"TR_ROLLOUT_OPERATION_ID": "recovery-operation-0002"},
    )
    assert refused.returncode != 0
    assert "explicit reconciled takeover" in refused.stderr
    assert _mutation_events(events) == []

    taken_over = _run_helper(
        tmp_path,
        manifest,
        state_path,
        events,
        "rollback",
        extra_env={
            "TR_ROLLOUT_OPERATION_ID": "recovery-operation-0002",
            "TR_ROLLOUT_TAKEOVER_EXPIRED_LEASE": "true",
        },
    )
    assert taken_over.returncode == 0, taken_over.stderr
    assert json.loads(promotion_path.read_text(encoding="utf-8"))["lease"] is None
    assert _mutation_events(events) == []


def test_provider_timeout_kills_command_and_permanently_fences_takeover(
    tmp_path: Path,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["sleep_before"] = [
        "run services update-traffic trusted-router-public"
    ]
    state["sleep_seconds"] = 30
    state_path.write_text(json.dumps(state), encoding="utf-8")

    timed_out = _run_helper(
        tmp_path,
        manifest,
        state_path,
        events,
        "promote-step",
        "primary",
        "10",
        extra_env={
            "TR_ROLLOUT_OPERATION_ID": "timed-operation-0001",
            "TR_ROLLOUT_LEASE_TTL_SECONDS": "60",
            "TR_ROLLOUT_PROVIDER_MUTATION_TIMEOUT_SECONDS": "5",
        },
    )
    assert timed_out.returncode != 0
    assert "lease-bounded deadline" in timed_out.stderr
    assert _candidate_percents(state_path) == {0}

    promotion_path = tmp_path / "promotion-state.json"
    promotion = json.loads(promotion_path.read_text(encoding="utf-8"))
    assert promotion["lease"]["owner"] == "timed-operation-0001"
    assert promotion["lease"]["mutation"]["operation"] == "traffic"
    promotion["lease"]["expires_at"] = "2000-01-01T00:00:00+00:00"
    promotion_path.write_text(json.dumps(promotion), encoding="utf-8")
    promotion_path.chmod(0o600)
    mutation_events_before = _mutation_events(events)

    refused = _run_helper(
        tmp_path,
        manifest,
        state_path,
        events,
        "rollback",
        extra_env={
            "TR_ROLLOUT_OPERATION_ID": "recovery-operation-0002",
            "TR_ROLLOUT_TAKEOVER_EXPIRED_LEASE": "true",
        },
    )
    assert refused.returncode != 0
    assert "unresolved provider mutation" in refused.stderr
    assert _mutation_events(events) == mutation_events_before


def test_durable_pre_provider_record_allows_fresh_runner_rollback(
    tmp_path: Path,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    _configure_durable_state(state_path, manifest)
    _set_service_candidate_percent(
        state_path, "trusted-router-public", "us-central1", 10
    )
    public_attempt = _traffic_state_attempts(manifest, 10)[0]
    public_attempt["operation"] = "traffic"
    _put_durable_journal(state_path, manifest, [public_attempt])

    result = _run_helper(
        tmp_path,
        manifest,
        state_path,
        events,
        "rollback",
        durable=True,
    )

    assert result.returncode == 0, result.stderr
    assert _candidate_percents(state_path) == {0}


@pytest.mark.parametrize(
    ("current", "target", "expected_updates"),
    [(10, 50, 6), (50, 50, 0), (50, 100, 6), (100, 100, 0)],
)
def test_promote_step_resumes_monotonically_at_every_boundary(
    tmp_path: Path,
    current: int,
    target: int,
    expected_updates: int,
) -> None:
    manifest, state, events = _fixture(tmp_path, regions=["us-central1"])
    _set_candidate_percent(state, current)
    _write_local_promotion_state(
        manifest,
        _traffic_state_attempts(manifest, current),
    )
    result = _run_helper(
        tmp_path,
        manifest,
        state,
        events,
        "promote-step",
        "primary",
        str(target),
    )
    assert result.returncode == 0, result.stderr
    assert _candidate_percents(state) == {target}
    updates = [
        line
        for line in events.read_text(encoding="utf-8").splitlines()
        if "run services update-traffic" in line
    ]
    assert len(updates) == expected_updates


def test_invalid_promotion_transition_does_zero_mutations(tmp_path: Path) -> None:
    manifest, state, events = _fixture(tmp_path, regions=["us-central1"])
    result = _run_helper(
        tmp_path,
        manifest,
        state,
        events,
        "promote-step",
        "primary",
        "50",
    )
    assert result.returncode != 0
    assert "out of order" in result.stderr
    assert not any(
        "run services update-traffic" in line
        for line in events.read_text(encoding="utf-8").splitlines()
    )


def test_nonzero_after_apply_is_accepted_only_after_postcondition(tmp_path: Path) -> None:
    manifest, state_path, events = _fixture(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["fail_after"] = ["update-traffic trusted-router-public --region=us-central1"]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = _run_helper(tmp_path, manifest, state_path, events, "promote")
    assert result.returncode == 0, result.stderr
    assert "inspecting provider state" in result.stderr
    assert _candidate_percents(state_path) == {100}


def test_mid_cohort_failure_rolls_back_only_recorded_attempts_and_surfaces_failure(tmp_path: Path) -> None:
    manifest, state_path, events = _fixture(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["fail_before"] = [
        "update-traffic trusted-router-actions --region=us-central1 --to-revisions=trusted-router-actions-candidate=50"
    ]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = _run_helper(tmp_path, manifest, state_path, events, "promote")
    assert result.returncode != 0
    assert _candidate_percents(state_path) == {0}
    promotion_state = json.loads((tmp_path / "promotion-state.json").read_text(encoding="utf-8"))
    attempted = {
        (item["service"], item["region"])
        for item in promotion_state["attempts"]
        if item["operation"] == "traffic"
    }
    assert ("trusted-router-public", "us-central1") in attempted
    assert not any(region == "europe-west4" for _, region in attempted)


def test_explicit_rollback_surfaces_partial_restore_failure(tmp_path: Path) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    promoted = _run_helper(
        tmp_path,
        manifest,
        state_path,
        events,
        "promote-step",
        "primary",
        "10",
    )
    assert promoted.returncode == 0, promoted.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["fail_before"] = [
        "update-traffic trusted-router-public --region=us-central1 "
        "--to-revisions=trusted-router-public-prior=100"
    ]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = _run_helper(tmp_path, manifest, state_path, events, "rollback")
    assert result.returncode != 0
    assert "one or more attempted companion traffic states failed" in result.stderr
    assert _candidate_percents(state_path) == {0, 10}


def test_rollback_restores_duplicate_zero_percent_tag_target_exactly(tmp_path: Path) -> None:
    manifest_path, state_path, events = _fixture(
        tmp_path,
        regions=["us-central1"],
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    public_entry = next(
        item for item in manifest["services"] if item["surface"] == "public"
    )
    prior = public_entry["prior_traffic"][0]["revision"]
    public_entry["prior_traffic"].append(
        {
            "revision": prior,
            "resolved_revision": prior,
            "latest_revision": False,
            "percent": 0,
            "tag": "stable",
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _run_state("validate-manifest", manifest_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    public = state["services"]["trusted-router-public|us-central1"]
    public["spec"]["traffic"].append(
        {"revisionName": prior, "percent": 0, "tag": "stable"}
    )
    public["status"]["traffic"].append(
        {"revisionName": prior, "percent": 0, "tag": "stable"}
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")

    promoted = _run_helper(
        tmp_path,
        manifest_path,
        state_path,
        events,
        "promote-step",
        "primary",
        "10",
    )
    assert promoted.returncode == 0, promoted.stderr
    restored = _run_helper(tmp_path, manifest_path, state_path, events, "rollback")
    assert restored.returncode == 0, restored.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["services"]["trusted-router-public|us-central1"]["status"][
        "traffic"
    ] == [
        {"revisionName": prior, "percent": 100},
        {"revisionName": prior, "percent": 0, "tag": "stable"},
    ]


def test_unknown_url_map_refuses_rollback_without_mutation(tmp_path: Path) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["live_map"] = "unknown"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = _run_helper(tmp_path, manifest, state_path, events, "rollback")
    assert result.returncode != 0
    assert "unknown third state" in result.stderr
    assert not any(
        "update-traffic" in line or "url-maps import" in line
        for line in events.read_text(encoding="utf-8").splitlines()
    )


def test_initial_split_cuts_over_only_by_url_map(tmp_path: Path) -> None:
    manifest, state, events = _fixture(tmp_path, "initial_split")
    result = _run_helper(tmp_path, manifest, state, events, "promote")
    assert result.returncode == 0, result.stderr
    lines = events.read_text(encoding="utf-8").splitlines()
    companion_smoke = lines.index("smoke initial-companions 100")
    assert lines.index("smoke preflight 0") < companion_smoke
    map_import = next(index for index, line in enumerate(lines) if "url-maps import" in line)
    map_smoke = lines.index("smoke initial-map 100")
    assert companion_smoke < map_import < map_smoke
    assert not any("update-traffic" in line for line in lines)
    console_smoke = lines.index("smoke initial-console 100")
    assert map_smoke < console_smoke < len(lines) - 1
    assert any("run services describe" in line for line in lines[console_smoke + 1 :])


def test_initial_smoke_failure_never_imports_map_or_shifts_console(tmp_path: Path) -> None:
    manifest, state, events = _fixture(tmp_path, "initial_split")
    result = _run_helper(
        tmp_path,
        manifest,
        state,
        events,
        "promote",
        fail_smoke="initial-companions",
    )
    assert result.returncode != 0
    lines = events.read_text(encoding="utf-8").splitlines()
    assert not any("url-maps import" in line for line in lines)
    assert not any("update-traffic" in line for line in lines)
    # Every split candidate is 100%-but-unrouted, and the untouched legacy
    # monolith remains at its exact named 100%-serving fallback revision.
    assert _candidate_percents(state) == {100}


def test_initial_map_only_rollback_ignores_broken_split_candidate(
    tmp_path: Path,
) -> None:
    manifest, state_path, events = _fixture(
        tmp_path, "initial_split", regions=["us-central1"]
    )
    promoted = _run_helper(tmp_path, manifest, state_path, events, "promote")
    assert promoted.returncode == 0, promoted.stderr
    state = json.loads(state_path.read_text(encoding="utf-8"))
    split = state["services"]["trusted-router-public|us-central1"]
    split["status"]["conditions"] = [{"type": "Ready", "status": "False"}]
    split["spec"]["template"]["spec"]["containers"][0]["env"].append(
        {"name": "HOSTILE_DRIFT", "value": "true"}
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")

    events.write_text("", encoding="utf-8")
    rolled_back = _run_helper(tmp_path, manifest, state_path, events, "rollback")

    assert rolled_back.returncode == 0, rolled_back.stderr
    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["live_map"] == "prior"
    lines = events.read_text(encoding="utf-8").splitlines()
    assert any("url-maps import" in line for line in lines)
    assert not any("update-traffic" in line for line in lines)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state["services"]["trusted-router|us-central1"]["status"].update(
            conditions=[{"type": "Ready", "status": "False"}]
        ),
        lambda state: state["service_iam"].update(
            {
                "trusted-router|us-central1": {
                    "bindings": [
                        {
                            "role": "roles/run.invoker",
                            "members": ["allUsers"],
                            "condition": {"title": "expired", "expression": "false"},
                        }
                    ]
                }
            }
        ),
        lambda state: state["negs"][
            "trusted-router-control-neg|us-central1"
        ]["cloudRun"].update(service="trusted-router-console"),
    ],
)
def test_legacy_fallback_drift_blocks_initial_promotion_without_mutation(
    tmp_path: Path,
    mutation: Any,
) -> None:
    manifest, state_path, events = _fixture(
        tmp_path, "initial_split", regions=["us-central1"]
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state.setdefault("service_iam", {})
    mutation(state)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_helper(tmp_path, manifest, state_path, events, "promote")

    assert result.returncode != 0
    assert _mutation_events(events) == []


def test_initial_manifest_rejects_floating_latest_legacy_fallback(
    tmp_path: Path,
) -> None:
    manifest_path, _, _ = _fixture(
        tmp_path, "initial_split", regions=["us-central1"]
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    traffic = manifest["legacy_fallback"][0]["traffic"][0]
    traffic.update(revision=None, latest_revision=True)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_state("validate-manifest", manifest_path, check=False)

    assert result.returncode != 0
    assert "LATEST" in result.stderr


def test_initial_out_of_band_candidate_import_between_callback_and_cutover_is_rejected(
    tmp_path: Path,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, "initial_split")
    result = _run_helper(
        tmp_path,
        manifest,
        state_path,
        events,
        "promote",
        extra_env={"OOB_IMPORT_PHASE": "initial-companions"},
    )
    assert result.returncode != 0
    assert "without this manifest's recorded import attempt" in result.stderr
    assert not any(
        "url-maps import" in line or "update-traffic" in line
        for line in events.read_text(encoding="utf-8").splitlines()
    )


def test_out_of_band_candidate_map_and_invalid_verify_cohort_do_zero_mutations(tmp_path: Path) -> None:
    manifest, state_path, events = _fixture(tmp_path, "initial_split")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["live_map"] = "candidate"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = _run_helper(tmp_path, manifest, state_path, events, "promote")
    assert result.returncode != 0
    assert not any("update-traffic" in line or "url-maps import" in line for line in events.read_text(encoding="utf-8").splitlines())

    events.write_text("", encoding="utf-8")
    result = _run_helper(tmp_path, manifest, state_path, events, "verify", "bogus", "100")
    assert result.returncode != 0
    assert events.read_text(encoding="utf-8") == ""


def test_live_service_contract_drift_blocks_verify_without_mutation(tmp_path: Path) -> None:
    manifest, state_path, events = _fixture(tmp_path)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    service = state["services"]["trusted-router-chat|us-central1"]
    for item in service["spec"]["template"]["spec"]["containers"][0]["env"]:
        if item["name"] == "TR_MAX_REQUEST_BODY_BYTES":
            item["value"] = "4194304"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = _run_helper(tmp_path, manifest, state_path, events, "verify", "all", "0")
    assert result.returncode != 0
    assert not any("update-traffic" in line or "url-maps import" in line for line in events.read_text(encoding="utf-8").splitlines())


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda container, annotations: container["resources"]["limits"].update(
                memory="4Gi"
            ),
            "memory differs",
        ),
        (
            lambda container, annotations: container["ports"][0].update(
                containerPort=9090
            ),
            "container port differs",
        ),
        (
            lambda container, annotations: container["startupProbe"].update(
                timeoutSeconds=1
            ),
            "startup probe timeoutSeconds differs",
        ),
        (
            lambda container, annotations: annotations.update(
                {"run.googleapis.com/vpc-access-egress": "all-traffic"}
            ),
            "VPC egress differs",
        ),
    ],
)
def test_platform_contract_drift_blocks_verify_without_mutation(
    tmp_path: Path,
    mutate: Any,
    message: str,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    service = state["services"]["trusted-router-chat|us-central1"]
    template = service["spec"]["template"]
    container = template["spec"]["containers"][0]
    mutate(container, template["metadata"]["annotations"])
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_helper(tmp_path, manifest, state_path, events, "verify", "all", "0")

    assert result.returncode != 0
    assert message in result.stderr
    assert _mutation_events(events) == []


@pytest.mark.parametrize("drift", ("sidecar", "command", "volume"))
def test_container_shape_drift_blocks_verify_without_mutation(
    tmp_path: Path,
    drift: str,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    template_spec = state["services"]["trusted-router-chat|us-central1"]["spec"][
        "template"
    ]["spec"]
    if drift == "sidecar":
        template_spec["containers"].append(
            {"image": "example.invalid/sidecar@sha256:" + "2" * 64}
        )
    elif drift == "command":
        template_spec["containers"][0]["command"] = ["/bin/sh"]
        template_spec["containers"][0]["args"] = ["-c", "exit 0"]
    else:
        template_spec["volumes"] = [{"name": "unexpected", "emptyDir": {}}]
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_helper(tmp_path, manifest, state_path, events, "verify", "all", "0")

    assert result.returncode != 0
    assert _mutation_events(events) == []


def test_https_proxy_repoint_blocks_promotion_without_mutation(tmp_path: Path) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["proxy_map"] = "out-of-band-map"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_helper(tmp_path, manifest, state_path, events, "promote")

    assert result.returncode != 0
    assert "no longer targets manifest map" in result.stderr
    assert _mutation_events(events) == []


def test_extra_cloud_armor_priority_blocks_verify_without_mutation(tmp_path: Path) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["policies"]["trusted-router-public-edge"]["rules"].append(
        {
            "priority": 100,
            "action": "allow",
            "preview": False,
            "match": {"config": {"srcIpRanges": ["*"]}},
        }
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = _run_helper(tmp_path, manifest, state_path, events, "verify", "all", "0")
    assert result.returncode != 0
    assert "unexpected Cloud Armor priority set" in result.stderr
    assert _mutation_events(events) == []


def test_cloud_armor_header_injection_blocks_verify_without_mutation(
    tmp_path: Path,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    default_rule = next(
        rule
        for rule in state["policies"]["trusted-router-public-edge"]["rules"]
        if rule["priority"] == 2_147_483_647
    )
    default_rule["headerAction"] = {
        "requestHeadersToAdds": [
            {"headerName": "Authorization", "headerValue": "Bearer stale"}
        ]
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_helper(tmp_path, manifest, state_path, events, "verify", "all", "0")

    assert result.returncode != 0
    assert "retains forbidden fields" in result.stderr
    assert _mutation_events(events) == []


def test_second_url_map_reference_blocks_promotion_without_mutation(
    tmp_path: Path,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["other_url_maps"] = {
        "rogue-map": {
            "name": "rogue-map",
            "defaultService": (
                "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/"
                "global/backendServices/trusted-router-public-backend"
            ),
        }
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_helper(tmp_path, manifest, state_path, events, "verify", "all", "0")

    assert result.returncode != 0
    assert "also reachable from URL map rogue-map" in result.stderr
    assert not any(
        "update-traffic" in line or "url-maps import" in line
        for line in events.read_text(encoding="utf-8").splitlines()
    )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda state: state["backends"]["trusted-router-public-backend"].update(
                timeoutSec=61
            ),
            "timeout differs",
        ),
        (
            lambda state: state["backends"]["trusted-router-public-backend"][
                "backends"
            ][0].update(
                group=(
                    "https://www.googleapis.com/compute/v1/projects/other-project/"
                    "regions/us-central1/networkEndpointGroups/"
                    "trusted-router-public-neg"
                )
            ),
            "exact same-project regional inventory",
        ),
        (
            lambda state: state["backends"]["trusted-router-public-backend"].update(
                securityPolicy=(
                    "https://www.googleapis.com/compute/v1/projects/other-project/"
                    "global/securityPolicies/trusted-router-public-edge"
                )
            ),
            "wrong Cloud Armor policy",
        ),
        (
            lambda state: state["negs"][
                "trusted-router-public-neg|us-central1"
            ]["cloudRun"].update(tag="candidate"),
            "without tag/urlMask",
        ),
        (
            lambda state: state["negs"][
                "trusted-router-public-neg|us-central1"
            ]["cloudRun"].update(urlMask="example.com/<service>"),
            "without tag/urlMask",
        ),
        (
            lambda state: state["backends"]["trusted-router-public-backend"][
                "customRequestHeaders"
            ].append("Authorization:Bearer stale-internal-token"),
            "request-header allowlist",
        ),
        (
            lambda state: state["backends"]["trusted-router-public-backend"].update(
                iap={"enabled": True}
            ),
            "IAP contract is not disabled",
        ),
        (
            lambda state: state["backends"]["trusted-router-public-backend"].update(
                customResponseHeaders=["X-Internal-Token: stale"]
            ),
            "retains custom response headers",
        ),
        (
            lambda state: state["backends"]["trusted-router-public-backend"][
                "logConfig"
            ].update(sampleRate=1.0),
            "logging contract drifted",
        ),
        (
            lambda state: state["backends"]["trusted-router-public-backend"][
                "cdnPolicy"
            ]["cacheKeyPolicy"].update(includeHost=False),
            "CDN/cache-key contract drifted",
        ),
        (
            lambda state: state["backends"]["trusted-router-public-backend"].update(
                edgeSecurityPolicy="projects/other/global/securityPolicies/stale"
            ),
            "unexpected edge security policy",
        ),
    ],
)
def test_edge_resource_identity_drift_blocks_verify_without_mutation(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    mutation(state)
    state_path.write_text(json.dumps(state), encoding="utf-8")

    result = _run_helper(tmp_path, manifest, state_path, events, "verify", "all", "0")

    assert result.returncode != 0
    assert message in result.stderr
    assert not any(
        "update-traffic" in line or "url-maps import" in line
        for line in events.read_text(encoding="utf-8").splitlines()
    )


def test_manifest_rejects_positive_tagged_traffic_before_helper_math(
    tmp_path: Path,
) -> None:
    manifest_path, _, _ = _fixture(tmp_path, regions=["us-central1"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    public_entry = next(
        item for item in manifest["services"] if item["surface"] == "public"
    )
    public_entry["prior_traffic"][0]["tag"] = "stable"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = _run_state("validate-manifest", manifest_path, check=False)

    assert result.returncode != 0
    assert "positive traffic targets must be untagged" in result.stderr


def test_conditional_allusers_invoker_is_rejected_without_mutation(tmp_path: Path) -> None:
    manifest, state_path, events = _fixture(tmp_path, regions=["us-central1"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["service_iam"] = {
        "trusted-router-public|us-central1": {
            "bindings": [
                {
                    "role": "roles/run.invoker",
                    "members": ["allUsers"],
                    "condition": {
                        "title": "expires",
                        "expression": "request.time < timestamp('2026-08-21T00:00:00Z')",
                    },
                }
            ]
        }
    }
    state_path.write_text(json.dumps(state), encoding="utf-8")
    result = _run_helper(tmp_path, manifest, state_path, events, "verify", "all", "0")
    assert result.returncode != 0
    assert "unauthenticated LB invocation IAM drifted" in result.stderr
    assert not any(
        "update-traffic" in line or "url-maps import" in line
        for line in events.read_text(encoding="utf-8").splitlines()
    )


def test_traffic_state_preserves_duplicate_tag_target_and_diagnoses_latest(tmp_path: Path) -> None:
    service_path = tmp_path / "service.json"
    service_path.write_text(
        json.dumps(
            {
                "spec": {
                    "traffic": [
                        {"revisionName": "service-prior", "percent": 100},
                        {"revisionName": "service-prior", "percent": 0, "tag": "stable"},
                        {"latestRevision": True, "percent": 0, "tag": "floating"},
                    ]
                },
                "status": {
                    "latestReadyRevisionName": "service-candidate",
                    "traffic": [
                        {"revisionName": "service-prior", "percent": 100},
                        {"revisionName": "service-prior", "percent": 0, "tag": "stable"},
                        {"revisionName": "service-candidate", "percent": 0, "tag": "floating"},
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    traffic = json.loads(_run_state("traffic-state", service_path).stdout)
    assert len(traffic) == 3
    floating = next(item for item in traffic if item["tag"] == "floating")
    assert floating == {
        "revision": None,
        "resolved_revision": "service-candidate",
        "latest_revision": True,
        "percent": 0,
        "tag": "floating",
    }


def test_traffic_state_rejects_unconverged_named_desired_and_observed(tmp_path: Path) -> None:
    service_path = tmp_path / "service.json"
    service_path.write_text(
        json.dumps(
            {
                "spec": {"traffic": [{"revisionName": "service-a", "percent": 100}]},
                "status": {
                    "traffic": [{"revisionName": "service-b", "percent": 100}]
                },
            }
        ),
        encoding="utf-8",
    )
    result = _run_state("traffic-state", service_path, check=False)
    assert result.returncode != 0
    assert "have not converged" in result.stderr


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value["url_map"].update(candidate_snapshot="url-map.prior.json"), "distinct"),
        (lambda value: value.update(image="repo/image:mutable"), "immutable"),
        (
            lambda value: value["preserved_hosts"].remove("api-us-central1.quillrouter.com"),
            "regional quillrouter",
        ),
        (
            lambda value: value["services"][0]["prior_traffic"][0].update(
                resolved_revision="trusted-router-public-other"
            ),
            "resolved_revision",
        ),
        (
            lambda value: value["services"][0]["prior_traffic"][0].update(
                revision=None,
                latest_revision=True,
            ),
            "LATEST",
        ),
        (
            lambda value: value["services"][0]["prior_traffic"][0].update(
                tag="serving"
            ),
            "positive traffic targets must be untagged",
        ),
    ],
)
def test_manifest_mutations_fail_closed(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    manifest_path, _, _ = _fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutation(manifest)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    result = _run_state("validate-manifest", manifest_path, check=False)
    assert result.returncode != 0
    assert message in result.stderr


def test_manifest_and_promotion_state_never_contain_secret_material(tmp_path: Path) -> None:
    manifest_path, state, events = _fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    serialized = json.dumps(manifest)
    assert "secretKeyRef" not in serialized
    assert "/secrets/" not in serialized
    assert "TR_" not in serialized
    result = _run_helper(tmp_path, manifest_path, state, events, "promote-step", "primary", "10")
    assert result.returncode == 0, result.stderr
    promotion = (tmp_path / "promotion-state.json").read_text(encoding="utf-8")
    assert "secretKeyRef" not in promotion
    assert "/secrets/" not in promotion
    assert "TR_" not in promotion


def test_service_hash_covers_service_max_and_numeric_secret_version(tmp_path: Path) -> None:
    data, _, _ = _service_json("console", "us-central1")
    container = data["spec"]["template"]["spec"]["containers"][0]
    container["env"].append(
        {
            "name": "TR_STRIPE_SECRET_KEY",
            "valueFrom": {
                "secretKeyRef": {"name": "trustedrouter-stripe-secret-key", "key": "17"}
            },
        }
    )
    path = tmp_path / "service.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    original = _hash_json(path, "hash-service")
    data["metadata"]["annotations"]["run.googleapis.com/maxScale"] = "21"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert _hash_json(path, "hash-service") != original
    data["metadata"]["annotations"]["run.googleapis.com/maxScale"] = "20"
    container["env"][-1]["valueFrom"]["secretKeyRef"]["key"] = "18"
    path.write_text(json.dumps(data), encoding="utf-8")
    assert _hash_json(path, "hash-service") != original


def test_rollout_sources_pin_digest_numeric_secrets_and_exact_body_bulkheads() -> None:
    source = (ROOT / "scripts/deploy/rollout.sh").read_text(encoding="utf-8")
    assert "IMAGE=\"$RESOLVED_IMAGE\"" in source
    assert "fully_qualified_digest" in source
    assert "pin_surface_secret_versions" in source
    assert ":latest" not in source
    assert "TR_RATE_LIMIT_CLIENT_IP_MODE=edge_header" in source
    assert "TR_MAX_CONCURRENT_REQUEST_BODIES=${MAX_CONCURRENT_REQUEST_BODIES}" in source
    assert "TR_SYNTHETIC_SCHEDULER_INTERVAL_SECONDS=0" in source
    assert "TR_REMEDIATOR_IN_PROCESS_ENABLED=false" in source
    assert "--deploy-health-check" in source
    assert "httpGet.path=/ready" in source
