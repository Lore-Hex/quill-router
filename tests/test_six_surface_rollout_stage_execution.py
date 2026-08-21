"""End-to-end fake-provider execution of six-surface staging."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ROLLOUT = ROOT / "scripts/deploy/rollout.sh"
BOOTSTRAP = ROOT / "scripts/deploy/rollout_bootstrap_internal.sh"
FRONTEND_ATTEST = ROOT / "scripts/deploy/rollout_frontend_attest.py"
LEGACY_HARDENER = ROOT / "scripts/deploy/rollout_legacy_harden.py"
STATE_TOOL = ROOT / "scripts/deploy/rollout_state.py"
URL_MAP_TOOL = ROOT / "scripts/deploy/service_surface_url_map.py"

PROJECT = "quill-cloud-proxy"
REGIONS = ("us-central1", "europe-west4")
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
    "public": (
        "trusted-router-public-backend",
        "trusted-router-public-neg",
        "trusted-router-public-edge",
    ),
    "actions": (
        "trusted-router-actions-backend",
        "trusted-router-actions-neg",
        "trusted-router-actions-edge",
    ),
    "console": (
        "trusted-router-console-backend",
        "trusted-router-console-neg",
        "trusted-router-console-edge",
    ),
    "chat": (
        "trusted-router-chat-backend",
        "trusted-router-chat-neg",
        "trusted-router-chat-edge",
    ),
    "webhooks": (
        "trusted-router-webhooks-backend",
        "trusted-router-webhooks-neg",
        "trusted-router-webhooks-edge",
    ),
    "internal": (
        "trusted-router-billing-backend",
        "trusted-router-billing-neg",
        "trusted-router-billing-edge",
    ),
}
CONTRACTS = {
    "public": (4, 0, 10, 60),
    "actions": (4, 0, 2, 30),
    "console": (4, 1, 20, 300),
    "chat": (2, 1, 20, 300),
    "webhooks": (4, 1, 10, 60),
    "internal": (8, 2, 50, 300),
}
IMAGE = (
    "us-central1-docker.pkg.dev/quill-cloud-proxy/trusted-router/trusted-router"
    "@sha256:" + "a" * 64
)

PRESERVED_HOSTS = (
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
    *(f"api-{region}.quillrouter.com" for region in REGIONS),
)
FRONTEND_HOSTS = (
    "trustedrouter.com",
    "www.trustedrouter.com",
    "status.trustedrouter.com",
    "trust.trustedrouter.com",
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
)
FRONTEND_VIP = "34.111.20.30"


def _load_rewriter() -> Callable[..., dict[str, Any]]:
    spec = importlib.util.spec_from_file_location("stage_url_map", URL_MAP_TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.rewrite_url_map


def _load_legacy_hardener() -> Any:
    spec = importlib.util.spec_from_file_location("stage_legacy_hardener", LEGACY_HARDENER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policy() -> dict[str, Any]:
    allowed = (
        r"^(trustedrouter[.]com|www[.]trustedrouter[.]com|status[.]trustedrouter[.]com|"
        r"trust[.]trustedrouter[.]com|eu[.]trustedrouter[.]com|status-us[.]trustedrouter[.]com|"
        r"status-eu[.]trustedrouter[.]com|allyrouter[.]com|www[.]allyrouter[.]com|"
        r"status[.]allyrouter[.]com|trust[.]allyrouter[.]com|uptimerouter[.]com|"
        r"www[.]uptimerouter[.]com|status[.]uptimerouter[.]com|"
        r"trust[.]uptimerouter[.]com)(:[0-9]+)?$"
    )

    def throttle(priority: int, count: int, expression: str | None, preview: bool) -> dict[str, Any]:
        match = (
            {
                "config": {"srcIpRanges": ["*"]},
                "versionedExpr": "SRC_IPS_V1",
            }
            if expression is None
            else {"expr": {"expression": expression}}
        )
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
            throttle(1000, 120, "request.path.startsWith('/chat-proxy/')", True),
            throttle(
                1100,
                300,
                "request.method != 'GET' && request.method != 'HEAD' && "
                "request.method != 'OPTIONS'",
                True,
            ),
            throttle(1200, 2400, None, False),
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


def _binding(role: str, surfaces: tuple[str, ...]) -> dict[str, Any]:
    return {
        "role": role,
        "members": [
            f"serviceAccount:tr-{surface}@{PROJECT}.iam.gserviceaccount.com"
            for surface in surfaces
        ],
    }


def _prior_service(surface: str, region: str) -> dict[str, Any]:
    service = SERVICES[surface]
    revision = f"{service}-prior"
    annotations = {
        "run.googleapis.com/ingress": "internal-and-cloud-load-balancing",
        "run.googleapis.com/ingress-status": "internal-and-cloud-load-balancing",
        "run.googleapis.com/maxScale": str(CONTRACTS[surface][2]),
    }
    if surface not in {"console", "internal"}:
        annotations["run.googleapis.com/default-url-disabled"] = "true"
    return {
        "metadata": {"name": service, "generation": 4, "annotations": annotations},
        "spec": {
            "scaling": {"maxInstanceCount": CONTRACTS[surface][2]},
            "traffic": [{"revisionName": revision, "percent": 100}],
            "template": {"metadata": {"name": revision}, "spec": {"containers": [{}]}},
        },
        "status": {
            "observedGeneration": 4,
            "latestCreatedRevisionName": revision,
            "latestReadyRevisionName": revision,
            "conditions": [{"type": "Ready", "status": "True"}],
            "url": (
                f"https://{service}-123456789.{region}.run.app"
                if surface in {"console", "internal"}
                else ""
            ),
            "traffic": [{"revisionName": revision, "percent": 100}],
        },
    }


def _legacy_console_service(region: str) -> dict[str, Any]:
    service = json.loads(
        json.dumps(_prior_service("console", region)).replace(
            "trusted-router-console", "trusted-router"
        )
    )
    revision = _serving_console_revision()
    service["spec"]["template"]["spec"] = copy.deepcopy(revision["spec"])
    return service


def _serving_console_revision() -> dict[str, Any]:
    values = {
        "TR_NEW_SIGNUPS_ENABLED": "true",
        "TR_GOOGLE_OAUTH_LOGIN_AVAILABLE": "false",
        "TR_GITHUB_OAUTH_LOGIN_AVAILABLE": "false",
        "TR_PAYPAL_CHECKOUT_ENABLED": "false",
        "TR_ADYEN_ENABLED": "false",
        "TR_VERIFF_ENABLED": "false",
        "TR_STORAGE_BACKEND": "spanner-bigtable",
        "TR_REQUEST_RECORD_WRITE_MODE": "typed",
        "TR_ANALYTICS_READ_MODE": "bigtable",
        "TR_GENERATION_RECORDS_ENABLED": "true",
        "TR_BIGTABLE_MIRROR_WRITES_ENABLED": "true",
        "TR_PROVIDER_ANALYTICS_CLICKHOUSE_URL": "http://10.8.0.5:8123",
    }
    return {
        "metadata": {"name": "trusted-router-prior"},
        "spec": {
            "serviceAccountName": (
                "123456789-compute@developer.gserviceaccount.com"
            ),
            "containers": [
                {
                    "image": IMAGE,
                    "env": [
                        {"name": name, "value": value}
                        for name, value in values.items()
                    ],
                }
            ]
        },
        "status": {"conditions": [{"type": "Ready", "status": "True"}]},
    }


def _synthetic_inventory() -> list[tuple[str, str, str]]:
    return [
        (
            region,
            f"trusted-router-synthetic-{region}",
            f"trusted-router-synthetic-{region}-every-three-minutes",
        )
        for region in REGIONS
    ] + [
        (
            REGIONS[0],
            f"trusted-router-throughput-{REGIONS[0]}",
            f"trusted-router-throughput-{REGIONS[0]}-every-five-minutes",
        ),
        (
            REGIONS[0],
            f"trusted-router-image-generation-{REGIONS[0]}",
            f"trusted-router-image-generation-{REGIONS[0]}-every-six-hours",
        ),
        (
            REGIONS[0],
            f"trusted-router-video-generation-{REGIONS[0]}",
            f"trusted-router-video-generation-{REGIONS[0]}-daily",
        ),
    ]


def _synthetic_job(region: str, job_name: str) -> dict[str, Any]:
    base = f"https://trusted-router-billing-123456789.{region}.run.app"
    plain = {
        "TR_ENVIRONMENT": "worker",
        "TR_SERVICE_SURFACE": "observer",
        "TR_RELEASE": "stage-execution-test",
        "TR_ENABLE_LIVE_PROVIDERS": "false",
        "TR_API_BASE_URL": "https://api.trustedrouter.com/v1",
        "TR_TRUSTED_DOMAIN": "trustedrouter.com",
        "TR_STORAGE_BACKEND": "spanner-bigtable",
        "TR_GCP_PROJECT_ID": PROJECT,
        "TR_SPANNER_INSTANCE_ID": "trusted-router-nam6",
        "TR_SPANNER_DATABASE_ID": "trusted-router",
        "TR_BIGTABLE_INSTANCE_ID": "trusted-router-logs",
        "TR_BIGTABLE_GENERATION_TABLE": "trustedrouter-generations",
        "TR_REGIONS": ",".join(REGIONS),
        "TR_PRIMARY_REGION": REGIONS[0],
        "TR_SYNTHETIC_MONITOR_MODEL": "trustedrouter/monitor",
        "TR_SYNTHETIC_MONITOR_TIMEOUT_SECONDS": "30",
        "TR_SYNTHETIC_CONTROL_PLANE_URL": "https://trustedrouter.com",
        "TR_SYNTHETIC_RUNS_PER_INVOCATION": "1",
        "TR_SYNTHETIC_RUN_SPACING_SECONDS": "0",
        "VERTEX_PROJECT_ID": PROJECT,
        "VERTEX_LOCATION": "us-central1",
        "TR_SYNTHETIC_MONITOR_REGION": region,
        "TR_SYNTHETIC_INGEST_URL": f"{base}/v1/internal/synthetic/samples",
        "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL": base,
    }
    if job_name.startswith("trusted-router-synthetic-"):
        module, cpu, memory, timeout = (
            "trusted_router.synthetic.cli",
            "2",
            "1Gi",
            "300s",
        )
        plain.update(
            {
                "TR_SYNTHETIC_BENCHMARK_INGEST_URL": (
                    f"{base}/v1/internal/synthetic/benchmark"
                ),
                "TR_SYNTHETIC_ROUTE_HEALTH_URL": (
                    f"{base}/v1/internal/synthetic/route-health"
                ),
                "TR_SYNTHETIC_BILLING_CONCURRENCY": "2",
                "TR_SYNTHETIC_START_DELAY_SECONDS": str(REGIONS.index(region) * 20),
                "TR_SYNTHETIC_ROTATION_ENABLED": "true",
                "TR_SYNTHETIC_ROTATION_PER_PASS": "2",
                "TR_SYNTHETIC_THROUGHPUT_ENABLED": "false",
                "TR_SYNTHETIC_THROUGHPUT_ONLY": "false",
            }
        )
        if region == REGIONS[0]:
            plain["TR_SYNTHETIC_REMEDIATOR_URL"] = (
                f"{base}/v1/internal/synthetic/remediate"
            )
    elif job_name.startswith("trusted-router-throughput-"):
        module, cpu, memory, timeout = (
            "trusted_router.synthetic.cli",
            "1",
            "1Gi",
            "300s",
        )
        plain.update(
            {
                "TR_SYNTHETIC_BENCHMARK_INGEST_URL": (
                    f"{base}/v1/internal/synthetic/benchmark"
                ),
                "TR_SYNTHETIC_ROUTE_HEALTH_URL": (
                    f"{base}/v1/internal/synthetic/route-health"
                ),
                "TR_SYNTHETIC_BILLING_CONCURRENCY": "1",
                "TR_SYNTHETIC_START_DELAY_SECONDS": "0",
                "TR_SYNTHETIC_ROTATION_ENABLED": "false",
                "TR_SYNTHETIC_ROTATION_PER_PASS": "0",
                "TR_SYNTHETIC_THROUGHPUT_ENABLED": "true",
                "TR_SYNTHETIC_THROUGHPUT_ONLY": "true",
                "TR_SYNTHETIC_THROUGHPUT_REGION": region,
                "TR_SYNTHETIC_THROUGHPUT_ROUTE_LIMIT": "200",
                "TR_SYNTHETIC_THROUGHPUT_MAX_TOKENS": "512",
                "TR_SYNTHETIC_THROUGHPUT_MINIMUM_OUTPUT_TOKENS": "128",
                "TR_SYNTHETIC_THROUGHPUT_TIMEOUT_SECONDS": "90",
                "TR_SYNTHETIC_THROUGHPUT_TIMEOUT_CEILING_SECONDS": "210",
                "TR_SYNTHETIC_THROUGHPUT_INTERVAL_SECONDS": "300",
            }
        )
    elif job_name.startswith("trusted-router-image-generation-"):
        module, cpu, memory, timeout = (
            "trusted_router.synthetic.image_generation",
            "1",
            "512Mi",
            "300s",
        )
        plain.update(
            {
                "TR_SYNTHETIC_IMAGE_MODEL": "google/gemini-3.1-flash-image-preview",
                "TR_SYNTHETIC_IMAGE_PROVIDER": "google-ai-studio",
                "TR_SYNTHETIC_IMAGE_TIMEOUT_SECONDS": "120",
                "TR_SYNTHETIC_IMAGE_CONFIRMATION_DELAY_SECONDS": "2",
            }
        )
    else:
        module, cpu, memory, timeout = (
            "trusted_router.synthetic.video_generation",
            "1",
            "512Mi",
            "1200s",
        )
        plain.update(
            {
                "TR_SYNTHETIC_VIDEO_TIMEOUT_SECONDS": "900",
                "TR_SYNTHETIC_VIDEO_POLL_INTERVAL_SECONDS": "5",
            }
        )
    environment = [{"name": name, "value": value} for name, value in plain.items()]
    environment.extend(
        [
            {
                "name": "TR_OBSERVER_INTERNAL_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "trustedrouter-observer-internal-token",
                        "key": "7",
                    }
                },
            },
            {
                "name": "TR_SYNTHETIC_MONITOR_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "trustedrouter-synthetic-monitor-api-key",
                        "key": "7",
                    }
                },
            },
        ]
    )
    return {
        "metadata": {"name": job_name},
        "spec": {
            "template": {
                "spec": {
                    "taskCount": 1,
                    "parallelism": 1,
                    "template": {
                        "metadata": {
                            "annotations": {
                                "run.googleapis.com/network-interfaces": json.dumps(
                                    [{"network": "default", "subnetwork": "default"}],
                                    separators=(",", ":"),
                                ),
                                "run.googleapis.com/vpc-access-egress": (
                                    "private-ranges-only"
                                ),
                            }
                        },
                        "spec": {
                            "serviceAccountName": (
                                "tr-synthetic@quill-cloud-proxy.iam.gserviceaccount.com"
                            ),
                            "maxRetries": 0,
                            "timeoutSeconds": timeout,
                            "containers": [
                                {
                                    "image": IMAGE,
                                    "command": ["/app/.venv/bin/python"],
                                    "args": ["-m", module],
                                    "resources": {
                                        "limits": {"cpu": cpu, "memory": memory}
                                    },
                                    "env": environment,
                                }
                            ],
                        }
                    }
                }
            }
        }
    }


def _synthetic_scheduler(
    region: str, job_name: str, scheduler_name: str
) -> dict[str, Any]:
    if job_name.startswith("trusted-router-synthetic-"):
        schedule = "*/3 * * * *"
    elif job_name.startswith("trusted-router-throughput-"):
        schedule = "*/5 * * * *"
    elif job_name.startswith("trusted-router-image-generation-"):
        schedule = "17 */6 * * *"
    else:
        schedule = "41 9 * * *"
    return {
        "name": scheduler_name,
        "state": "ENABLED",
        "schedule": schedule,
        "timeZone": "Etc/UTC",
        "attemptDeadline": "300s",
        "retryConfig": {
            "retryCount": 0,
            "maxRetryDuration": "0s",
            "minBackoffDuration": "5s",
            "maxBackoffDuration": "60s",
            "maxDoublings": 3,
        },
        "httpTarget": {
            "uri": (
                f"https://{region}-run.googleapis.com/apis/run.googleapis.com/v1/"
                f"namespaces/{PROJECT}/jobs/{job_name}:run"
            ),
            "httpMethod": "POST",
            "oauthToken": {
                "serviceAccountEmail": (
                    "tr-synthetic@quill-cloud-proxy.iam.gserviceaccount.com"
                )
            },
        },
    }


def _initial_state() -> dict[str, Any]:
    backend_links = {
        surface: (
            f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/"
            f"backendServices/{EDGES[surface][0]}"
        )
        for surface in SURFACES
    }
    managed_hosts = [
        f"{prefix}{domain}"
        for domain in ("trustedrouter.com", "allyrouter.com", "uptimerouter.com")
        for prefix in ("", "www.", "status.", "trust.")
    ] + ["eu.trustedrouter.com", "status-us.trustedrouter.com", "status-eu.trustedrouter.com"]
    prior_map = {
        "name": "trusted-router-map",
        "selfLink": (
            f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/"
            "urlMaps/trusted-router-map"
        ),
        "defaultService": (
            f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/"
            "backendServices/trusted-router-control-backend"
        ),
        "hostRules": [
            {"hosts": list(PRESERVED_HOSTS), "pathMatcher": "preserved-api"},
            {"hosts": managed_hosts, "pathMatcher": "legacy-console"},
        ],
        "pathMatchers": [
            {"name": "preserved-api", "defaultService": "api-gateway-backend"},
            {
                "name": "legacy-console",
                "defaultService": (
                    f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/"
                    "backendServices/trusted-router-control-backend"
                ),
            },
        ],
    }
    candidate_map = _load_rewriter()(
        prior_map,
        backend_links["public"],
        backend_links["actions"],
        backend_links["console"],
        backend_links["chat"],
        backend_links["webhooks"],
        backend_links["internal"],
        ["trustedrouter.com", "allyrouter.com", "uptimerouter.com"],
        list(PRESERVED_HOSTS),
    )
    backends: dict[str, Any] = {}
    negs: dict[str, Any] = {}
    for surface in SURFACES:
        backend, neg, policy = EDGES[surface]
        timeout = CONTRACTS[surface][3]
        backends[backend] = {
            "name": backend,
            "selfLink": backend_links[surface],
            "loadBalancingScheme": "EXTERNAL_MANAGED",
            "protocol": "HTTP",
            "timeoutSec": timeout,
            "securityPolicy": (
                f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/"
                f"securityPolicies/{policy}"
            ),
            "customRequestHeaders": [
                "X-TrustedRouter-Client-IP:{client_ip_address}"
            ],
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
                        f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/regions/"
                        f"{region}/networkEndpointGroups/{neg}"
                    )
                }
                for region in REGIONS
            ],
        }
        for region in REGIONS:
            negs[f"{neg}|{region}"] = {"cloudRun": {"service": SERVICES[surface]}}
    backends["trusted-router-control-backend"] = {
        "name": "trusted-router-control-backend",
        "selfLink": (
            f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/"
            "backendServices/trusted-router-control-backend"
        ),
        "loadBalancingScheme": "EXTERNAL_MANAGED",
        "protocol": "HTTP",
        "timeoutSec": 300,
        "backends": [
            {
                "group": (
                    f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/regions/"
                    f"{region}/networkEndpointGroups/trusted-router-control-neg"
                )
            }
            for region in REGIONS
        ],
    }
    for region in REGIONS:
        negs[f"trusted-router-control-neg|{region}"] = {
            "networkEndpointType": "SERVERLESS",
            "cloudRun": {"service": "trusted-router"},
        }

    required_secret_owners = {
        "trustedrouter-attribution-cookie-secret": ("public", "console"),
        "trustedrouter-sentry-dsn": ("console", "chat", "webhooks", "internal"),
        "trustedrouter-stripe-secret-key": ("console",),
        "trustedrouter-internal-stripe-payment-intents-key": ("internal",),
        "trustedrouter-stripe-webhook-secret": ("webhooks",),
        "trustedrouter-internal-gateway-token": ("internal",),
        "trustedrouter-observer-internal-token": ("internal",),
        "trustedrouter-synthetic-monitor-api-key": ("internal",),
        "trustedrouter-aws-access-key-id": ("actions", "console"),
        "trustedrouter-aws-secret-access-key": ("actions", "console"),
        "trustedrouter-internal-ses-access-key-id": ("internal",),
        "trustedrouter-internal-ses-secret-access-key": ("internal",),
        "trustedrouter-ops-chat-webhook-secret": ("actions",),
        "trustedrouter-clickhouse-provider-read-password": ("console",),
        # Keep the optional console array non-empty on Bash 3.2; the fixture
        # also verifies that an optional mounted credential remains console-only.
        "trustedrouter-telnyx-api-key": ("console",),
    }
    synthetic_inventory = _synthetic_inventory()
    proxy_link = (
        f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/"
        "targetHttpsProxies/trusted-router-control-https-proxy"
    )
    certificate_link = (
        f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/"
        "sslCertificates/trusted-router-managed"
    )
    return {
        "url_map": prior_map,
        "expected_candidate_map": candidate_map,
        "forwarding_rule": {
            "name": "trusted-router-public-https",
            "selfLink": (
                f"https://www.googleapis.com/compute/v1/projects/{PROJECT}/global/"
                "forwardingRules/trusted-router-public-https"
            ),
            "IPAddress": FRONTEND_VIP,
            "IPProtocol": "TCP",
            "portRange": "443-443",
            "networkTier": "PREMIUM",
            "loadBalancingScheme": "EXTERNAL_MANAGED",
            "target": proxy_link,
        },
        "https_proxy_resource": {
            "name": "trusted-router-control-https-proxy",
            "selfLink": proxy_link,
            "urlMap": prior_map["selfLink"],
            "sslCertificates": [certificate_link],
        },
        "ssl_certificates": {
            "trusted-router-managed": {
                "name": "trusted-router-managed",
                "selfLink": certificate_link,
                "type": "MANAGED",
                "managed": {"status": "ACTIVE", "domains": list(FRONTEND_HOSTS)},
                "subjectAlternativeNames": list(FRONTEND_HOSTS),
            }
        },
        "dns": {
            host: {"A": [FRONTEND_VIP], "AAAA": []} for host in FRONTEND_HOSTS
        },
        "services": {
            f"trusted-router|{region}": _legacy_console_service(region)
            for region in REGIONS
        },
        "serving_console_revision": _serving_console_revision(),
        "backends": backends,
        "policies": {EDGES[surface][2]: _policy() for surface in SURFACES},
        "negs": negs,
        "required_secret_owners": {
            secret: list(owners) for secret, owners in required_secret_owners.items()
        },
        "latest_secret_versions": {},
        "jobs": {
            f"{job_name}|{region}": _synthetic_job(region, job_name)
            for region, job_name, _ in synthetic_inventory
        },
        "schedulers": {
            f"{scheduler_name}|{region}": _synthetic_scheduler(
                region, job_name, scheduler_name
            )
            for region, job_name, scheduler_name in synthetic_inventory
        },
        "iam": {
            "project": {
                "bindings": [
                    _binding(
                        "roles/serviceusage.serviceUsageConsumer",
                        ("public", "console", "chat", "webhooks", "internal"),
                    )
                ]
            },
            "spanner": {
                "bindings": [
                    _binding("roles/spanner.databaseReader", ("public", "chat")),
                    _binding(
                        "roles/spanner.databaseUser", ("console", "webhooks", "internal")
                    ),
                ]
            },
            "bigtable": {
                "bindings": [
                    _binding("roles/bigtable.reader", ("public", "console")),
                    _binding("roles/bigtable.user", ("internal",)),
                ]
            },
            "byok": {
                "bindings": [
                    _binding("roles/cloudkms.cryptoKeyEncrypterDecrypter", ("console",)),
                    _binding("roles/cloudkms.cryptoKeyDecrypter", ("internal",)),
                ]
            },
            "ads": {
                "bindings": [
                    _binding("roles/cloudkms.cryptoKeyEncrypter", ("console",))
                ]
            },
        },
        "deploy_fail_after_remaining": 1,
        "fatal_deploy_before_regions": [],
        "corrupt_traffic_after_deploy_service": "",
        "deployed_revisions": [],
        "deployments": [],
    }


FAKE_GCLOUD = r'''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state_path = Path(os.environ["FAKE_GCLOUD_STATE"])
events_path = Path(os.environ["FAKE_GCLOUD_EVENTS"])
state = json.loads(state_path.read_text(encoding="utf-8"))
args = sys.argv[1:]
if args[:1] == ["--project"]:
    args = args[2:]
joined = " ".join(args)
with events_path.open("a", encoding="utf-8") as output:
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
        if isinstance(value, str):
            print(value)
        else:
            print(json.dumps(value, separators=(",", ":")))
    raise SystemExit(code)

def missing():
    print("NOT_FOUND", file=sys.stderr)
    finish(code=1)

def member(surface):
    return f"serviceAccount:tr-{surface}@quill-cloud-proxy.iam.gserviceaccount.com"

def require_stage_journal():
    expected = os.environ.get("EXPECTED_STAGE_JOURNAL")
    if not expected:
        return
    path = Path(expected)
    if not path.is_file() or path.stat().st_mode & 0o777 != 0o600:
        print("initial-stage mutation preceded its mode-0600 journal", file=sys.stderr)
        finish(code=95)

if args[:2] == ["projects", "describe"]:
    finish("123456789")
if args[:3] == ["compute", "forwarding-rules", "describe"]:
    finish(state["forwarding_rule"])
if args[:3] == ["compute", "target-https-proxies", "describe"]:
    if option("--format") == "json":
        finish(state["https_proxy_resource"])
    finish("trusted-router-map")
if args[:3] == ["compute", "ssl-certificates", "describe"]:
    finish(state["ssl_certificates"][args[3]])
if args[:3] == ["iam", "service-accounts", "describe"]:
    finish({"email": args[3], "disabled": False})
if args[:3] == ["iam", "service-accounts", "list"]:
    finish([
        {"email": f"tr-{surface}@quill-cloud-proxy.iam.gserviceaccount.com"}
        for surface in (
            "public", "actions", "console", "chat", "webhooks", "internal", "synthetic"
        )
    ])
if args[:3] == ["iam", "service-accounts", "get-iam-policy"]:
    finish({"bindings": [{"role": "roles/iam.serviceAccountUser", "members": [
        "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
    ]}]})
if args[:3] == ["compute", "url-maps", "describe"]:
    finish(state["url_map"])
if args[:3] == ["compute", "url-maps", "list"]:
    finish("trusted-router-map")
if args[:3] == ["compute", "url-maps", "validate"]:
    finish()
if args[:3] == ["compute", "url-maps", "import"]:
    finish(code=97)
if args[:3] == ["run", "services", "list"]:
    finish([
        {
            "metadata": {
                "name": key.split("|", 1)[0],
                "labels": {"cloud.googleapis.com/location": key.split("|", 1)[1]},
            }
        }
        for key in state["services"]
    ])
if args[:3] == ["run", "services", "describe"]:
    key = f"{args[3]}|{option('--region')}"
    if key not in state["services"]:
        missing()
    finish(state["services"][key])
if args[:3] == ["run", "services", "get-iam-policy"]:
    finish({"bindings": [{"role": "roles/run.invoker", "members": ["allUsers"]}]})
if args[:3] == ["run", "revisions", "describe"]:
    revision_key = f"{args[3]}|{option('--region')}"
    if revision_key in state.get("deployed_revisions", []):
        finish({"metadata": {"name": args[3]}})
    if args[3].endswith(("-rstage1", "-rbad1")):
        missing()
    finish(state["serving_console_revision"])
if args[:3] == ["run", "services", "update-traffic"]:
    key = f"{args[3]}|{option('--region')}"
    service = state["services"][key]
    assignments = option("--to-revisions").split(",")
    traffic = []
    for assignment in assignments:
        revision, percent = assignment.rsplit("=", 1)
        traffic.append({"revisionName": revision, "percent": int(percent)})
    service["spec"]["traffic"] = traffic
    service["status"]["traffic"] = traffic
    state["services"][key] = service
    finish()
if args[:3] == ["run", "jobs", "describe"]:
    finish(state["jobs"][f"{args[3]}|{option('--region')}"])
if args[:3] == ["run", "jobs", "get-iam-policy"]:
    finish({"bindings": [{
        "role": "roles/run.invoker",
        "members": [
            "serviceAccount:tr-synthetic@quill-cloud-proxy.iam.gserviceaccount.com"
        ],
    }]})
if args[:3] == ["run", "jobs", "list"]:
    region = option("--region")
    finish([
        {"metadata": {"name": key.split("|", 1)[0]}}
        for key in state["jobs"]
        if key.endswith("|" + region)
    ])
if args[:3] == ["scheduler", "jobs", "describe"]:
    finish(state["schedulers"][f"{args[3]}|{option('--location')}"])
if args[:3] == ["scheduler", "jobs", "list"]:
    region = option("--location")
    finish([
        value for key, value in state["schedulers"].items()
        if key.endswith("|" + region)
    ])
if args[:2] == ["run", "deploy"]:
    require_stage_journal()
    service = args[2]
    region = option("--region")
    key = f"{service}|{region}"
    if region in state.get("fatal_deploy_before_regions", []):
        finish(code=88)
    previous = state["services"].get(key)
    if previous is None and "--no-traffic" in args:
        print("--no-traffic not supported when creating a new service", file=sys.stderr)
        finish(code=96)
    account = option("--service-account")
    surface = account.split("@", 1)[0].removeprefix("tr-")
    candidate = f"{service}-{option('--revision-suffix')}"
    delimiter_value = option("--set-env-vars")
    delimiter = delimiter_value[1:delimiter_value.index("^", 1)]
    raw_environment = delimiter_value[len(delimiter) + 2:]
    values = dict(item.split("=", 1) for item in raw_environment.split(delimiter))
    secrets = {}
    raw_secrets = option("--set-secrets") or ""
    for item in filter(None, raw_secrets.split(",")):
        name, reference = item.split("=", 1)
        resource, version = reference.rsplit(":", 1)
        secrets[name] = (resource, version)
    env = [{"name": name, "value": value} for name, value in values.items()]
    env.extend({
        "name": name,
        "valueFrom": {"secretKeyRef": {"name": resource, "key": version}},
    } for name, (resource, version) in secrets.items())
    maximum = int(option("--max"))
    minimum = int(option("--min-instances"))
    default_disabled = "--no-default-url" in args
    annotations = {
        "run.googleapis.com/ingress": option("--ingress"),
        "run.googleapis.com/ingress-status": option("--ingress"),
        "run.googleapis.com/maxScale": str(maximum),
    }
    if default_disabled:
        annotations["run.googleapis.com/default-url-disabled"] = "true"
    prior_traffic = (
        previous["status"]["traffic"]
        if previous is not None
        else [{"revisionName": candidate, "percent": 100}]
    )
    generation = int(previous["metadata"].get("generation", 0)) + 1 if previous else 1
    state["services"][key] = {
        "metadata": {"name": service, "generation": generation, "annotations": annotations},
        "spec": {
            "scaling": {"maxInstanceCount": maximum},
            "traffic": prior_traffic,
            "template": {
                "metadata": {"name": candidate, "annotations": {
                    "autoscaling.knative.dev/minScale": str(minimum),
                    "autoscaling.knative.dev/maxScale": str(maximum),
                    "run.googleapis.com/network-interfaces": json.dumps(
                        [{
                            "network": option("--network"),
                            "subnetwork": option("--subnet"),
                        }],
                        separators=(",", ":"),
                    ),
                    "run.googleapis.com/vpc-access-egress": option("--vpc-egress"),
                }},
                "spec": {
                    "serviceAccountName": account,
                    "containerConcurrency": int(option("--concurrency")),
                    "timeoutSeconds": option("--timeout"),
                    "containers": [{
                        "image": option("--image"),
                        "ports": [{
                            "containerPort": int(option("--port")),
                            "name": "http1",
                        }],
                        "resources": {"limits": {
                            "cpu": option("--cpu") or "1",
                            "memory": option("--memory"),
                        }},
                        "startupProbe": {
                            "httpGet": {"path": "/ready", "port": int(option("--port"))},
                            "initialDelaySeconds": 0,
                            "timeoutSeconds": 10,
                            "periodSeconds": 10,
                            "failureThreshold": 18,
                        },
                        "env": env,
                    }],
                },
            },
        },
        "status": {
            "observedGeneration": generation,
            "latestCreatedRevisionName": candidate,
            "latestReadyRevisionName": candidate,
            "conditions": [{"type": "Ready", "status": "True"}],
            "url": (
                ""
                if default_disabled
                else f"https://{service}-123456789.{region}.run.app"
            ),
            "traffic": prior_traffic,
        },
    }
    state["deployments"].append(key)
    revision_key = f"{candidate}|{region}"
    if revision_key not in state["deployed_revisions"]:
        state["deployed_revisions"].append(revision_key)
    if state.get("corrupt_traffic_after_deploy_service") == service:
        old_revision = prior_traffic[0]["revisionName"]
        corrupted = [
            {"revisionName": old_revision, "percent": 90},
            {"revisionName": candidate, "percent": 10, "tag": "hostile"},
        ]
        state["services"][key]["spec"]["traffic"] = corrupted
        state["services"][key]["status"]["traffic"] = corrupted
        state["corrupt_traffic_after_deploy_service"] = ""
        finish(code=1)
    if state["deploy_fail_after_remaining"]:
        state["deploy_fail_after_remaining"] -= 1
        finish(code=1)
    finish()
if args[:3] == ["projects", "get-iam-policy", "quill-cloud-proxy"]:
    finish(state["iam"]["project"])
if args[:3] == ["spanner", "instances", "list"]:
    finish([{"name": "projects/quill-cloud-proxy/instances/trusted-router-nam6"}])
if args[:3] == ["spanner", "databases", "list"]:
    finish([{"name": "projects/quill-cloud-proxy/instances/trusted-router-nam6/databases/trusted-router"}])
if args[:3] == ["spanner", "instances", "get-iam-policy"]:
    finish({"bindings": []})
if args[:3] == ["spanner", "databases", "get-iam-policy"]:
    finish(state["iam"]["spanner"])
if args[:3] == ["bigtable", "instances", "list"]:
    finish([{"name": "projects/quill-cloud-proxy/instances/trusted-router-logs"}])
if args[:3] == ["bigtable", "tables", "list"]:
    finish([{"name": "projects/quill-cloud-proxy/instances/trusted-router-logs/tables/trustedrouter-generations"}])
if args[:3] == ["bigtable", "instances", "get-iam-policy"]:
    finish(state["iam"]["bigtable"])
if args[:3] == ["bigtable", "tables", "get-iam-policy"]:
    finish({"bindings": []})
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
    finish({"bindings": []})
if args[:3] == ["kms", "keys", "get-iam-policy"]:
    finish(state["iam"]["byok" if args[3] == "byok-envelope" else "ads"])
if args[:3] == ["projects", "get-ancestors", "quill-cloud-proxy"]:
    finish([{"type": "project", "id": "quill-cloud-proxy"}])
if args[:2] == ["secrets", "describe"]:
    finish({"name": args[2]}) if args[2] in state["required_secret_owners"] else missing()
if args[:2] == ["secrets", "list"]:
    finish([{"name": name} for name in state["required_secret_owners"]])
if args[:3] == ["secrets", "versions", "describe"]:
    secret = option("--secret")
    if secret not in state["required_secret_owners"]:
        missing()
    requested = args[3]
    version = (
        str(state.get("latest_secret_versions", {}).get(secret, 7))
        if requested == "latest"
        else requested
    )
    finish({"state": "ENABLED", "name": f"projects/quill-cloud-proxy/secrets/{secret}/versions/{version}"})
if args[:3] == ["secrets", "get-iam-policy", args[2] if len(args) > 2 else ""]:
    secret = args[2]
    owners = state["required_secret_owners"].get(secret)
    if owners is None:
        missing()
    members = [member(surface) for surface in owners]
    if secret == "trustedrouter-telnyx-api-key":
        members.append(
            "serviceAccount:tr-deploy@quill-cloud-proxy.iam.gserviceaccount.com"
        )
    if secret in {
        "trustedrouter-observer-internal-token",
        "trustedrouter-synthetic-monitor-api-key",
    }:
        members.append(
            "serviceAccount:tr-synthetic@quill-cloud-proxy.iam.gserviceaccount.com"
        )
    finish({"bindings": [{
        "role": "roles/secretmanager.secretAccessor",
        "members": members,
    }]})
if args[:4] == ["artifacts", "docker", "images", "describe"]:
    finish({"image_summary": {"fully_qualified_digest": os.environ["IMAGE"]}})
if args[:4] == ["compute", "networks", "subnets", "describe"]:
    finish({
        "privateIpGoogleAccess": True,
        "network": "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/global/networks/default",
    })
if args[:3] == ["dns", "managed-zones", "describe"]:
    finish({
        "dnsName": "run.app.",
        "visibility": "private",
        "privateVisibilityConfig": {"networks": [{
            "networkUrl": "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/global/networks/default"
        }]},
    })
if args[:3] == ["dns", "record-sets", "describe"]:
    if args[3] == "run.app.":
        finish({"rrdatas": ["199.36.153.11", "199.36.153.9", "199.36.153.8", "199.36.153.10"]})
    finish({"rrdatas": ["run.app."]})
if args[:3] == ["compute", "backend-services", "describe"]:
    backend = state["backends"][args[3]]
    output_format = option("--format") or ""
    if "value(selfLink)" in output_format:
        finish(backend["selfLink"])
    if "value(securityPolicy.basename())" in output_format:
        finish(backend["securityPolicy"].rstrip("/").split("/")[-1])
    finish(backend)
if args[:3] == ["compute", "backend-services", "list"]:
    finish(list(state["backends"].values()))
if args[:3] == ["compute", "backend-services", "update"]:
    require_stage_journal()
    backend = state["backends"][args[3]]
    if option("--timeout"):
        backend["timeoutSec"] = int(option("--timeout").removesuffix("s"))
    if "--enable-cdn" in args:
        backend["enableCDN"] = True
        backend["compressionMode"] = option("--compression-mode") or "AUTOMATIC"
        backend["cdnPolicy"] = {
            "cacheMode": option("--cache-mode") or "USE_ORIGIN_HEADERS",
            "negativeCaching": "--no-negative-caching" not in args,
            "serveWhileStale": int(option("--serve-while-stale") or "0"),
            "cacheKeyPolicy": {
                "includeHost": "--cache-key-include-host" in args,
                "includeProtocol": "--cache-key-include-protocol" in args,
                "includeQueryString": "--cache-key-include-query-string" in args,
                "queryStringBlacklist": [],
                "queryStringWhitelist": [],
            },
        }
    if "--no-enable-cdn" in args:
        backend["enableCDN"] = False
    if "--no-negative-caching" in args:
        backend.setdefault("cdnPolicy", {})["negativeCaching"] = False
    if "--enable-logging" in args:
        backend["logConfig"] = {
            "enable": True,
            "sampleRate": float(option("--logging-sample-rate") or "1"),
            "optionalMode": "EXCLUDE_ALL_OPTIONAL",
        }
    if option("--security-policy"):
        backend["securityPolicy"] = (
            "https://www.googleapis.com/compute/v1/projects/quill-cloud-proxy/global/"
            f"securityPolicies/{option('--security-policy')}"
        )
    headers = [item.split("=", 1)[1] for item in args if item.startswith("--custom-request-header=")]
    if headers:
        backend["customRequestHeaders"] = headers
    finish()
if args[:3] == ["compute", "network-endpoint-groups", "describe"]:
    finish(state["negs"][f"{args[3]}|{option('--region')}"])
if args[:3] == ["compute", "security-policies", "describe"]:
    finish(state["policies"][args[3]])
if args[:3] == ["compute", "security-policies", "import"]:
    require_stage_journal()
    state["policies"][args[3]] = json.loads(
        Path(option("--file-name")).read_text(encoding="utf-8")
    )
    finish()
if args[:4] == ["compute", "security-policies", "rules", "describe"]:
    policy = option("--security-policy")
    priority = int(args[4])
    finish(next(item for item in state["policies"][policy]["rules"] if item["priority"] == priority))
if args[:4] == ["compute", "security-policies", "rules", "update"]:
    require_stage_journal()
    finish()
print("unsupported fake gcloud: " + joined, file=sys.stderr)
finish(code=2)
'''


FAKE_JQ = r'''#!/usr/bin/env python3
import json
import os
import re
import sys

args = sys.argv[1:]
letters = "".join(arg[1:] for arg in args if arg.startswith("-") and not arg.startswith("--"))
raw = "r" in letters
compact = "c" in letters
exit_status = "e" in letters
slurp = "s" in letters
raw_input = "R" in letters
null_input = "n" in letters
variables = {}
position = 0
while position < len(args):
    item = args[position]
    if item == "--arg":
        variables[args[position + 1]] = args[position + 2]
        position += 3
    elif item == "--argjson":
        variables[args[position + 1]] = json.loads(args[position + 2])
        position += 3
    elif item.startswith("-"):
        position += 1
    else:
        break
query = args[position]
files = args[position + 1:]
text = (
    ""
    if null_input and not files and "inputs" not in query
    else open(files[-1], encoding="utf-8").read()
    if files
    else sys.stdin.read()
)
if raw_input:
    data = text
elif slurp:
    data = [json.loads(line) for line in text.splitlines() if line.strip()]
elif null_input:
    data = None
else:
    data = json.loads(text)
normalized = " ".join(query.split())
with open(os.environ["FAKE_GCLOUD_EVENTS"], "a", encoding="utf-8") as output:
    output.write("jq " + normalized + "\n")
stream = False

def direct_bindings():
    return sorted({
        (binding.get("role"), json.dumps(binding.get("condition"), sort_keys=True))
        for binding in data.get("bindings", [])
        if variables["member"] in binding.get("members", [])
    })

if normalized.startswith(".email == $email and"):
    result = data.get("email") == variables["email"] and not data.get("disabled", False)
elif "roles/iam.serviceAccountUser" in normalized and normalized.startswith("([.bindings"):
    result = direct_bindings() == [("roles/iam.serviceAccountUser", "null")]
elif "roles/secretmanager.secretAccessor" in normalized and normalized.startswith("([.bindings"):
    result = direct_bindings() == [("roles/secretmanager.secretAccessor", "null")]
elif normalized == 'any(.pathMatchers[]?; .name == "trusted-router-service-surfaces")':
    result = any(item.get("name") == "trusted-router-service-surfaces" for item in data.get("pathMatchers", []))
elif normalized == "any(.[]; .latest_revision)":
    result = any(item.get("latest_revision") for item in data)
elif "console traffic is not one unambiguous 100% revision" in normalized:
    live = [item for item in data.get("status", {}).get("traffic", []) if int(item.get("percent", 0)) > 0]
    if len(live) != 1 or live[0].get("percent") != 100 or not live[0].get("revisionName"):
        raise SystemExit(1)
    result = live[0]["revisionName"]
elif "legacy fallback is not one named untagged 100% target" in normalized:
    traffic = data.get("status", {}).get("traffic", [])
    if (
        len(traffic) != 1
        or traffic[0].get("percent") != 100
        or traffic[0].get("tag") is not None
        or not traffic[0].get("revisionName")
    ):
        raise SystemExit(1)
    result = traffic[0]["revisionName"]
elif "legacy fallback generation is not observed" in normalized:
    generation = data.get("metadata", {}).get("generation")
    observed = data.get("status", {}).get("observedGeneration")
    if not isinstance(generation, int) or isinstance(generation, bool):
        raise SystemExit(1)
    if generation != observed:
        raise SystemExit(1)
    result = generation
elif "bootstrap artifact has inexact regional service inventory" in normalized:
    matches = [
        item.get("revision")
        for item in data.get("services", [])
        if item.get("region") == variables["region"]
    ]
    if len(matches) != 1:
        raise SystemExit(1)
    result = matches[0]
elif ".spec.containers[0].env" in normalized and ".value][0] // $default" in normalized:
    result = next((item.get("value") for item in data["spec"]["containers"][0].get("env", []) if item.get("name") == variables["name"] and "valueFrom" not in item), variables["default"])
elif ".spec.containers[0].env" in normalized and ".valueFrom.secretKeyRef.name" in normalized:
    result = next((item["valueFrom"]["secretKeyRef"].get("name", "").split("/")[-1] for item in data["spec"]["containers"][0].get("env", []) if item.get("name") == variables["name"] and "valueFrom" in item), "")
elif normalized.startswith("[ .bindings") or (normalized.startswith("[.bindings") and "| unique" in normalized):
    result = [{"role": role, "condition": json.loads(condition)} for role, condition in direct_bindings()]
elif normalized == '[{role:$role,condition:null}]':
    result = [{"role": variables["role"], "condition": None}]
elif normalized == ".[] | [.type, (.id|tostring)] | @tsv":
    result = [f"{item['type']}\t{item['id']}" for item in data]
    stream = True
elif normalized == ".state":
    result = data.get("state")
elif normalized == ".postcondition_sha256":
    result = data.get("postcondition_sha256")
elif normalized in {
    '.candidate_snapshot_sha256 // ""',
    '.manifest_sha256 // ""',
}:
    result = data.get(normalized.split()[0].removeprefix(".")) or ""
elif normalized == 'select(.state == "ENABLED") | .name | split("/")[-1]':
    result = data.get("name", "").split("/")[-1] if data.get("state") == "ENABLED" else None
elif normalized == ".name":
    result = data.get("name")
elif "fully_qualified_digest" in normalized:
    summary = data.get("image_summary") or data.get("imageSummary") or {}
    result = summary.get("fully_qualified_digest") or summary.get("fullyQualifiedDigest") or data.get("fully_qualified_digest") or data.get("fullyQualifiedDigest")
elif normalized.startswith(".privateIpGoogleAccess == true"):
    network = data.get("network", "").rstrip("/")
    result = data.get("privateIpGoogleAccess") is True and network.endswith("/networks/" + variables["network"])
elif normalized.startswith('.dnsName == "run.app."'):
    networks = data.get("privateVisibilityConfig", {}).get("networks", [])
    result = data.get("dnsName") == "run.app." and data.get("visibility") == "private" and any(item.get("networkUrl", "").rstrip("/").endswith("/networks/" + variables["network"]) for item in networks)
elif normalized.startswith("(.rrdatas | sort) =="):
    result = sorted(data.get("rrdatas", [])) == sorted(["199.36.153.8", "199.36.153.9", "199.36.153.10", "199.36.153.11"])
elif normalized == '.rrdatas == ["run.app."]':
    result = data.get("rrdatas") == ["run.app."]
elif normalized == ".":
    result = data
elif re.fullmatch(r"\.[a-z_]+(?:\.[a-z_]+)*", normalized):
    result = data
    for field in normalized.removeprefix(".").split("."):
        result = result[field]
elif normalized == "any(.[]; .name == $name)":
    result = any(item.get("name") == variables["name"] for item in data)
elif normalized == ".selfLink":
    result = data.get("selfLink")
elif "endswith(\"/regions/\" + $region" in normalized and "| length" in normalized:
    suffix = f"/regions/{variables['region']}/networkEndpointGroups/{variables['neg']}"
    result = sum(1 for item in data.get("backends", []) if item.get("group", "").endswith(suffix))
elif normalized.startswith('(\"/projects/\" + $project') and "| @tsv" in normalized:
    exact = (
        f"/projects/{variables['project']}/regions/{variables['region']}"
        f"/networkEndpointGroups/{variables['neg']}"
    )
    suffix = (
        f"/regions/{variables['region']}/networkEndpointGroups/{variables['neg']}"
    )
    groups = [item.get("group", "") for item in data.get("backends", [])]
    exact_count = sum(1 for group in groups if group == exact.lstrip("/") or group.endswith(exact))
    wrong_count = sum(1 for group in groups if group.endswith(suffix) and not group.endswith(exact))
    result = f"{exact_count}\t{wrong_count}"
elif normalized == ".backends[]?.group":
    result = [item.get("group") for item in data.get("backends", [])]
    stream = True
elif raw_input and "networkEndpointGroups" in normalized and "$neg" in normalized:
    regions = [line for line in data.splitlines() if line]
    result = sorted(f"/regions/{region}/networkEndpointGroups/{variables['neg']}" for region in regions)
elif normalized.startswith("[.backends[]?.group | capture"):
    result = sorted(re.search(r"(/regions/[^/]+/networkEndpointGroups/[^/]+)$", item["group"]).group(1) for item in data.get("backends", []))
elif normalized == ".cloudRun.service // \"\"":
    result = (data.get("cloudRun") or {}).get("service", "")
elif "select((.securityPolicy" in normalized and "join(\",\")" in normalized:
    result = ",".join(sorted({item["name"] for item in data if item.get("securityPolicy", "").split("/")[-1] == variables["policy"]}))
elif raw_input and "capture(\"^(?<name>[^=]+)=(?<value>.*)$\")" in normalized:
    result = dict(line.split("=", 1) for line in data.splitlines() if line)
elif raw_input and "(?<resource>.*):(?<version>" in normalized:
    result = {}
    for line in data.splitlines():
        if not line:
            continue
        name, reference = line.split("=", 1)
        resource, version = reference.rsplit(":", 1)
        result[name] = {"resource": resource, "version": version}
elif 'select(any(.members[]?; . == "allUsers"))' in normalized:
    bindings = [binding for binding in data.get("bindings", []) if "allUsers" in binding.get("members", [])]
    if "allUsersCount" in normalized:
        result = [
            {
                "role": binding.get("role"),
                "condition": binding.get("condition"),
                "allUsersCount": binding.get("members", []).count("allUsers"),
            }
            for binding in bindings
        ] == [{
            "role": "roles/run.invoker",
            "condition": None,
            "allUsersCount": 1,
        }]
    else:
        result = sorted({binding.get("role") for binding in bindings}) == [
            "roles/run.invoker"
        ]
elif normalized.startswith("{service:$service,backend:$backend"):
    result = {
        "service": variables["service"],
        "backend": variables["backend"],
        "region": variables["region"],
        "generation": variables["generation"],
        "serving_revision": variables["serving_revision"],
        "serving_revision_sha256": variables["serving_revision_sha256"],
        "traffic": variables["traffic"],
        "postcondition_sha256": variables["postcondition_sha256"],
        "backend_postcondition_sha256": variables[
            "backend_postcondition_sha256"
        ],
        "invoker_iam_sha256": variables["invoker_iam_sha256"],
    }
elif normalized.startswith("{surface:$surface,name:$name"):
    result = {
        "surface": variables["surface"],
        "name": variables["name"],
        "region": variables["region"],
        "prior_exists": variables["prior_exists"],
        "prior_traffic": variables["prior_traffic"],
        "adopted_bootstrap": variables["adopted_bootstrap"],
        "candidate_revision": variables["candidate_revision"],
        "runtime_service_account": variables["runtime_service_account"],
        "ingress": variables["ingress"],
        "default_url_disabled": variables["default_url_disabled"],
        "concurrency": variables["concurrency"],
        "min_instances": variables["min_instances"],
        "service_max_instances": variables["service_max_instances"],
        "revision_max_instances": variables["revision_max_instances"],
        "timeout_seconds": variables["timeout_seconds"],
        "memory": variables["memory"],
        "cpu": variables["cpu"],
        "container_port": variables["container_port"],
        "vpc_network": variables["vpc_network"],
        "vpc_subnet": variables["vpc_subnet"],
        "vpc_egress": variables["vpc_egress"],
        "startup_probe_path": variables["startup_probe_path"],
        "startup_probe_initial_delay_seconds": variables[
            "startup_probe_initial_delay_seconds"
        ],
        "startup_probe_timeout_seconds": variables[
            "startup_probe_timeout_seconds"
        ],
        "startup_probe_period_seconds": variables[
            "startup_probe_period_seconds"
        ],
        "startup_probe_failure_threshold": variables[
            "startup_probe_failure_threshold"
        ],
        "max_request_body_bytes": variables["max_request_body_bytes"],
        "max_in_flight_request_body_bytes": variables["max_in_flight_request_body_bytes"],
        "max_concurrent_request_bodies": variables["max_concurrent_request_bodies"],
        "request_body_read_timeout_seconds": variables["request_body_read_timeout_seconds"],
        "postcondition_sha256": variables["postcondition_sha256"],
    }
else:
    print("unsupported fake jq: " + normalized, file=sys.stderr)
    raise SystemExit(2)

truthy = result is not False and result is not None
if exit_status and not truthy:
    raise SystemExit(1)
items = result if stream else [result]
for item in items:
    if raw and isinstance(item, bool):
        print("true" if item else "false")
    elif raw and isinstance(item, (str, int, float)):
        print(item)
    else:
        print(json.dumps(item, separators=(",", ":") if compact else None))
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


def _install_fakes(tmp_path: Path) -> Path:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    for name, source in (
        ("gcloud", FAKE_GCLOUD),
        ("jq", FAKE_JQ),
        ("dig", FAKE_DIG),
    ):
        path = fake_bin / name
        path.write_text(source, encoding="utf-8")
        path.chmod(0o755)
    return fake_bin


def _write_legacy_hardening_artifact(state_path: Path) -> Path:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    hardener = _load_legacy_hardener()
    policy = {
        "bindings": [
            {
                "role": "roles/run.invoker",
                "members": ["allUsers"],
                "condition": None,
            }
        ]
    }
    iam_sha256 = hardener._sha(policy)  # noqa: SLF001
    revision = state["serving_console_revision"]
    revision_sha256 = hardener._sha(  # noqa: SLF001
        hardener._revision_semantic(revision)  # noqa: SLF001
    )
    artifact = {
        "schema_version": 1,
        "kind": "trusted-router-legacy-hardening-artifact",
        "project_id": PROJECT,
        "service": "trusted-router",
        "runtime_service_account": (
            "123456789-compute@developer.gserviceaccount.com"
        ),
        "operation_id": "legacy-hardener-test-operation",
        "revision_suffix": "prior",
        "regions": [
            {
                "region": region,
                "serving_revision": "trusted-router-prior",
                "service_sha256": hardener._sha(  # noqa: SLF001
                    hardener._service_semantic(  # noqa: SLF001
                        state["services"][f"trusted-router|{region}"]
                    )
                ),
                "revision_sha256": revision_sha256,
                "iam_sha256": iam_sha256,
            }
            for region in REGIONS
        ],
        "secret_refs": [],
        "journal_sha256": "0" * 64,
        "created_at": "2026-08-21T00:00:00+00:00",
    }
    path = state_path.with_name("legacy-hardening.json")
    path.write_text(json.dumps(artifact, sort_keys=True), encoding="utf-8")
    path.chmod(0o600)
    return path


def _capture_frontend_attestation(
    state_path: Path, fake_bin: Path, environment: dict[str, str]
) -> Path:
    artifact = state_path.with_name("frontend-attestation.json")
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(FRONTEND_ATTEST),
            "capture",
            "--project",
            PROJECT,
            "--forwarding-rule",
            "trusted-router-public-https",
            "--https-proxy",
            "trusted-router-control-https-proxy",
            "--url-map",
            "trusted-router-map",
            "--hosts",
            ",".join(FRONTEND_HOSTS),
            "--artifact",
            str(artifact),
        ],
        env={**environment, "PATH": f"{fake_bin}:{environment['PATH']}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert artifact.stat().st_mode & 0o777 == 0o600
    return artifact


def _fake_environment(
    fake_bin: Path,
    state_path: Path,
    events_path: Path,
    *,
    bootstrap_suffix: str = "iboot1",
    rollout_suffix: str = "rstage1",
) -> dict[str, str]:
    environment = {
        **{key: value for key, value in os.environ.items() if not key.startswith("TR_")},
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "FAKE_GCLOUD_STATE": str(state_path),
        "FAKE_GCLOUD_EVENTS": str(events_path),
        "PROJECT_ID": PROJECT,
        "TR_CONTROL_PLANE_REGIONS": ",".join(REGIONS),
        "TR_REGIONS": ",".join(REGIONS),
        "TR_PRIMARY_REGION": REGIONS[0],
        "TR_SYNTHETIC_MONITOR_REGIONS": ",".join(REGIONS),
        "TR_SYNTHETIC_THROUGHPUT_REGION": REGIONS[0],
        "TR_SYNTHETIC_IMAGE_REGION": REGIONS[0],
        "TR_SYNTHETIC_VIDEO_REGION": REGIONS[0],
        "TR_RELEASE": "stage-execution-test",
        "TR_ROLLOUT_REVISION_SUFFIX": rollout_suffix,
        "TR_ROLLOUT_OPERATION_ID": "stage-execution-operation",
        "TR_INTERNAL_BOOTSTRAP_REVISION_SUFFIX": bootstrap_suffix,
        "TR_ROLLOUT_LOCAL_LOCK_PATH": str(
            state_path.with_name("rollout-operation.lock")
        ),
        "IMAGE": IMAGE,
    }
    legacy_artifact = _write_legacy_hardening_artifact(state_path)
    frontend_artifact = _capture_frontend_attestation(
        state_path, fake_bin, environment
    )
    environment["TR_LEGACY_HARDENING_ARTIFACT"] = str(legacy_artifact)
    environment["TR_ROLLOUT_FRONTEND_ATTESTATION"] = str(frontend_artifact)
    return environment


def test_bootstrap_rejects_unrecorded_internal_service_before_mutation(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "fake-cloud.json"
    events_path = tmp_path / "gcloud-events.log"
    state = _initial_state()
    state["deploy_fail_after_remaining"] = 0
    state["services"][f"trusted-router-billing|{REGIONS[0]}"] = _prior_service(
        "internal", REGIONS[0]
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    fake_bin = _install_fakes(tmp_path)
    env = _fake_environment(fake_bin, state_path, events_path)
    artifact = tmp_path / "bootstrap" / "internal.json"

    rejected = subprocess.run(  # noqa: S603
        ["/bin/bash", str(BOOTSTRAP), "--artifact", str(artifact)],
        env=env,
        capture_output=True,
        text=True,
    )

    assert rejected.returncode != 0
    assert "unrecorded internal service" in rejected.stderr
    assert not artifact.exists()
    assert not Path(f"{artifact}.state").exists()
    events = events_path.read_text(encoding="utf-8").splitlines()
    assert not any(
        " run deploy " in f" {line} " or " update-traffic " in f" {line} "
        for line in events
    )


def test_bootstrap_reads_legacy_monolith_without_new_console_dependency(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "fake-cloud.json"
    events_path = tmp_path / "gcloud-events.log"
    state = _initial_state()
    state["deploy_fail_after_remaining"] = 0
    assert all(
        not key.startswith("trusted-router-console|") for key in state["services"]
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    fake_bin = _install_fakes(tmp_path)
    env = _fake_environment(fake_bin, state_path, events_path)
    env["TR_CONSOLE_SERVICE"] = "trusted-router-console"
    env["TR_LEGACY_CONSOLE_SERVICE"] = "trusted-router"
    env["LEGACY_CONSOLE_SERVICE"] = "trusted-router"
    artifact = tmp_path / "bootstrap" / "internal.json"

    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(BOOTSTRAP), "--artifact", str(artifact)],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    events = events_path.read_text(encoding="utf-8").splitlines()
    legacy_describes = [
        line
        for line in events
        if "run services describe trusted-router --region=" in line
    ]
    # Bootstrap reads the legacy monolith in every region — hardening-artifact
    # verification plus the data-mode preflight, all read-only describes. The
    # count per region is symmetric but not pinned: it moves whenever a
    # verification pass is added, and the property being proved is coverage
    # plus read-only-ness, not the number of reads.
    described_regions = {
        line.split("--region=", 1)[1].split()[0] for line in legacy_describes
    }
    assert described_regions == set(REGIONS)
    per_region = [
        sum(f"--region={region}" in line for line in legacy_describes)
        for region in REGIONS
    ]
    assert len(set(per_region)) == 1, per_region
    assert not any("trusted-router-console" in line for line in events)
    assert not any("run deploy trusted-router " in line for line in events)
    assert not any("update-traffic trusted-router " in line for line in events)


def test_bootstrap_rejects_path_bearing_private_health_origin(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "fake-cloud.json"
    events_path = tmp_path / "gcloud-events.log"
    state_path.write_text(json.dumps(_initial_state()), encoding="utf-8")
    fake_bin = _install_fakes(tmp_path)
    env = _fake_environment(fake_bin, state_path, events_path)
    artifact = tmp_path / "bootstrap" / "internal.json"

    bootstrap = subprocess.run(  # noqa: S603
        ["/bin/bash", str(BOOTSTRAP), "--artifact", str(artifact)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert bootstrap.returncode == 0, bootstrap.stderr

    state = json.loads(state_path.read_text(encoding="utf-8"))
    key = f"trusted-router-synthetic-{REGIONS[0]}|{REGIONS[0]}"
    env_items = state["jobs"][key]["spec"]["template"]["spec"]["template"][
        "spec"
    ]["containers"][0]["env"]
    health_origin = next(
        item
        for item in env_items
        if item["name"] == "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL"
    )
    health_origin["value"] += "/healthz"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    event_start = len(events_path.read_text(encoding="utf-8").splitlines())

    rejected = subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            str(BOOTSTRAP),
            "--verify-artifact",
            str(artifact),
            "--expected-image",
            IMAGE,
        ],
        env=env,
        capture_output=True,
        text=True,
    )

    assert rejected.returncode != 0
    rejected_events = events_path.read_text(encoding="utf-8").splitlines()[
        event_start:
    ]
    assert not any(
        " run deploy " in f" {line} "
        or " update-traffic " in f" {line} "
        or " url-maps import " in f" {line} "
        or " backend-services update " in f" {line} "
        for line in rejected_events
    )


def test_bootstrap_resumes_journal_and_initial_stage_never_deploys_legacy(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "fake-cloud.json"
    events_path = tmp_path / "gcloud-events.log"
    state = _initial_state()
    state["deploy_fail_after_remaining"] = 0
    state["fatal_deploy_before_regions"] = [REGIONS[0]]
    state_path.write_text(json.dumps(state), encoding="utf-8")
    fake_bin = _install_fakes(tmp_path)
    env = _fake_environment(fake_bin, state_path, events_path)
    artifact = tmp_path / "bootstrap" / "internal.json"
    journal = Path(f"{artifact}.state")

    interrupted = subprocess.run(  # noqa: S603
        ["/bin/bash", str(BOOTSTRAP), "--artifact", str(artifact)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert interrupted.returncode != 0
    assert not artifact.exists()
    interrupted_state = json.loads(journal.read_text(encoding="utf-8"))
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert interrupted_state["revision_suffix"] == "iboot1"
    assert interrupted_state["region_states"] == [
        {"region": REGIONS[0], "state": "deploy_intent"},
        {"region": REGIONS[1], "state": "pending"},
    ]

    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["fatal_deploy_before_regions"] = []
    state_path.write_text(json.dumps(state), encoding="utf-8")
    resume_env = dict(env)
    resume_env.pop("TR_INTERNAL_BOOTSTRAP_REVISION_SUFFIX")
    resumed = subprocess.run(  # noqa: S603
        ["/bin/bash", str(BOOTSTRAP), "--artifact", str(artifact)],
        env=resume_env,
        capture_output=True,
        text=True,
    )
    assert resumed.returncode == 0, resumed.stderr
    settled_state = json.loads(journal.read_text(encoding="utf-8"))
    assert {item["state"] for item in settled_state["region_states"]} == {"settled"}
    assert json.loads(artifact.read_text(encoding="utf-8"))["image"] == IMAGE

    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["corrupt_traffic_after_deploy_service"] = "trusted-router-public"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    manifest = tmp_path / "resumed-stage" / "manifest.json"
    stage_journal = Path(f"{manifest}.stage.state")
    drift_env = dict(env)
    drift_env["TR_INTERNAL_BOOTSTRAP_ARTIFACT"] = str(artifact)
    drift_env["TR_ROLLOUT_REVISION_SUFFIX"] = "rbad1"
    drift_env["EXPECTED_STAGE_JOURNAL"] = str(stage_journal)
    event_start = len(events_path.read_text(encoding="utf-8").splitlines())
    interrupted_stage = subprocess.run(  # noqa: S603
        ["/bin/bash", str(ROLLOUT), "--manifest", str(manifest)],
        env=drift_env,
        capture_output=True,
        text=True,
    )
    assert interrupted_stage.returncode != 0
    assert not manifest.exists()
    assert stage_journal.exists(), interrupted_stage.stderr
    assert stat.S_IMODE(stage_journal.stat().st_mode) == 0o600
    interrupted_stage_state = json.loads(stage_journal.read_text(encoding="utf-8"))
    public_us = next(
        item
        for item in interrupted_stage_state["service_states"]
        if item["surface"] == "public" and item["region"] == REGIONS[0]
    )
    assert public_us["state"] == "deploy_intent"
    assert public_us["candidate_revision"] == "trusted-router-public-rbad1"

    # The provider applied the immutable candidate and then returned nonzero,
    # but also changed traffic/tag state. Staging must reject that ambiguous
    # result. Once the exact off-map sole-candidate state is independently
    # restored, a fresh process adopts only the journal-owned same suffix.
    state = json.loads(state_path.read_text(encoding="utf-8"))
    public_key = f"trusted-router-public|{REGIONS[0]}"
    repaired_traffic = [
        {"revisionName": "trusted-router-public-rbad1", "percent": 100}
    ]
    state["services"][public_key]["spec"]["traffic"] = repaired_traffic
    state["services"][public_key]["status"]["traffic"] = repaired_traffic
    state_path.write_text(json.dumps(state), encoding="utf-8")
    resume_stage_env = dict(drift_env)
    resume_stage_env.pop("TR_ROLLOUT_REVISION_SUFFIX")
    resumed_stage = subprocess.run(  # noqa: S603
        ["/bin/bash", str(ROLLOUT), "--manifest", str(manifest)],
        env=resume_stage_env,
        capture_output=True,
        text=True,
    )
    assert resumed_stage.returncode == 0, resumed_stage.stderr
    assert manifest.exists()
    completed_stage_state = json.loads(stage_journal.read_text(encoding="utf-8"))
    assert completed_stage_state["phase"] == "complete"
    assert {item["state"] for item in completed_stage_state["service_states"]} == {
        "staged"
    }
    final_fake_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_fake_state["deployments"].count(public_key) == 1
    assert all(
        revision.split("|", 1)[0].endswith("-rbad1")
        or "-iboot1|" in revision
        for revision in final_fake_state["deployed_revisions"]
    )
    stage_events = events_path.read_text(encoding="utf-8").splitlines()[event_start:]
    assert not any("run deploy trusted-router " in line for line in stage_events)
    assert not any(
        " update-traffic " in f" {line} " or " url-maps import " in f" {line} "
        for line in stage_events
    )


def test_bootstrap_preserves_reviewed_spanner_clickhouse_mode(tmp_path: Path) -> None:
    state_path = tmp_path / "fake-cloud.json"
    events_path = tmp_path / "gcloud-events.log"
    state = _initial_state()
    state["deploy_fail_after_remaining"] = 0
    serving_env = state["serving_console_revision"]["spec"]["containers"][0]["env"]
    plain = {item["name"]: item for item in serving_env if "valueFrom" not in item}
    plain["TR_STORAGE_BACKEND"]["value"] = "spanner-clickhouse"
    plain["TR_ANALYTICS_READ_MODE"]["value"] = "clickhouse-only"
    plain["TR_BIGTABLE_MIRROR_WRITES_ENABLED"]["value"] = "false"
    serving_env.extend(
        [
            {
                "name": "TR_ANALYTICS_DUAL_READ_STARTED_AT",
                "value": "2026-08-01T00:00:00Z",
            },
            {
                "name": "TR_ANALYTICS_CLICKHOUSE_PRIMARY_STARTED_AT",
                "value": "2026-08-02T00:00:00Z",
            },
            {
                "name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL",
                "value": "http://10.8.0.5:8123",
            },
            {
                "name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER",
                "value": "tr_control_read",
            },
            {
                "name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE",
                "value": "tr",
            },
            {
                "name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": "trustedrouter-clickhouse-control-read-password",
                        "key": "7",
                    }
                },
            },
        ]
    )
    state["required_secret_owners"][
        "trustedrouter-clickhouse-control-read-password"
    ] = ["public", "console", "internal"]
    state["latest_secret_versions"][
        "trustedrouter-clickhouse-control-read-password"
    ] = 8
    state_path.write_text(json.dumps(state), encoding="utf-8")
    fake_bin = _install_fakes(tmp_path)
    env = _fake_environment(fake_bin, state_path, events_path)
    artifact_path = tmp_path / "bootstrap" / "internal.json"

    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(BOOTSTRAP), "--artifact", str(artifact_path)],
        env=env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["data_mode"] == {
        "storage_backend": "spanner-clickhouse",
        "analytics_read_mode": "clickhouse-only",
        "request_record_write_mode": "typed",
        "generation_records_enabled": "true",
        "bigtable_mirror_writes_enabled": "false",
        "analytics_dual_read_started_at": "2026-08-01T00:00:00Z",
        "analytics_clickhouse_primary_started_at": "2026-08-02T00:00:00Z",
        "operational_clickhouse_url": "http://10.8.0.5:8123",
        "operational_clickhouse_user": "tr_control_read",
        "operational_clickhouse_database": "tr",
    }
    deployed = json.loads(state_path.read_text(encoding="utf-8"))["services"][
        f"trusted-router-billing|{REGIONS[0]}"
    ]
    deployed_env = {
        item["name"]: item
        for item in deployed["spec"]["template"]["spec"]["containers"][0]["env"]
    }
    assert deployed_env["TR_STORAGE_BACKEND"]["value"] == "spanner-clickhouse"
    assert deployed_env["TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD"][
        "valueFrom"
    ]["secretKeyRef"] == {
        "name": "trustedrouter-clickhouse-control-read-password",
        "key": "7",
    }


def test_staging_reconciles_and_postverifies_all_six_surfaces(tmp_path: Path) -> None:
    state_path = tmp_path / "fake-cloud.json"
    events_path = tmp_path / "gcloud-events.log"
    manifest_path = tmp_path / "rollout" / "manifest.json"
    state_path.write_text(json.dumps(_initial_state()), encoding="utf-8")
    fake_bin = _install_fakes(tmp_path)
    env = _fake_environment(fake_bin, state_path, events_path)
    bootstrap_artifact = tmp_path / "bootstrap" / "internal.json"
    bootstrap_result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(BOOTSTRAP), "--artifact", str(bootstrap_artifact)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert bootstrap_result.returncode == 0, bootstrap_result.stderr
    assert "deploy exited non-zero; inspecting exact postconditions" in (
        bootstrap_result.stderr
    )
    artifact = json.loads(bootstrap_artifact.read_text(encoding="utf-8"))
    artifact_text = bootstrap_artifact.read_text(encoding="utf-8")
    bootstrap_state = Path(f"{bootstrap_artifact}.state")
    state_journal = json.loads(bootstrap_state.read_text(encoding="utf-8"))
    assert stat.S_IMODE(bootstrap_artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(bootstrap_state.stat().st_mode) == 0o600
    assert state_journal["revision_suffix"] == "iboot1"
    assert state_journal["image"] == IMAGE
    assert state_journal["regions"] == list(REGIONS)
    assert {item["state"] for item in state_journal["region_states"]} == {
        "settled"
    }
    assert "secret" not in artifact_text.lower()
    assert "trustedrouter-" not in artifact_text
    assert artifact["regions"] == list(REGIONS)
    assert artifact["internal_service"] == "trusted-router-billing"
    assert artifact["synthetic_service_account"] == (
        "tr-synthetic@quill-cloud-proxy.iam.gserviceaccount.com"
    )
    assert len(artifact["services"]) == len(REGIONS)
    bootstrap_events = events_path.read_text(encoding="utf-8").splitlines()
    assert not any("run deploy trusted-router " in line for line in bootstrap_events)
    assert [
        line for line in bootstrap_events if "run services update-traffic" in line
    ] == [
        (
            "gcloud run services update-traffic trusted-router-billing "
            f"--region={region} --clear-tags "
            "--to-revisions=trusted-router-billing-iboot1=100 --quiet"
        )
        for region in REGIONS
    ]

    clean_verified_state = json.loads(state_path.read_text(encoding="utf-8"))

    def assert_artifact_verify_rejects(mutator: Callable[[dict[str, Any]], None]) -> None:
        hostile_state = copy.deepcopy(clean_verified_state)
        mutator(hostile_state)
        state_path.write_text(json.dumps(hostile_state), encoding="utf-8")
        event_start = len(events_path.read_text(encoding="utf-8").splitlines())
        rejected = subprocess.run(  # noqa: S603
            [
                "/bin/bash",
                str(BOOTSTRAP),
                "--verify-artifact",
                str(bootstrap_artifact),
                "--expected-image",
                IMAGE,
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert rejected.returncode != 0
        rejected_events = events_path.read_text(encoding="utf-8").splitlines()[
            event_start:
        ]
        assert not any(
            " run deploy " in f" {line} "
            or " update-traffic " in f" {line} "
            or " url-maps import " in f" {line} "
            or " backend-services update " in f" {line} "
            for line in rejected_events
        )

    internal_key = f"trusted-router-billing|{REGIONS[0]}"

    def internal_container(value: dict[str, Any]) -> dict[str, Any]:
        return value["services"][internal_key]["spec"]["template"]["spec"][
            "containers"
        ][0]

    assert_artifact_verify_rejects(
        lambda value: value["services"][internal_key]["spec"]["template"][
            "spec"
        ]["containers"].append({"image": IMAGE})
    )
    assert_artifact_verify_rejects(
        lambda value: internal_container(value).__setitem__("args", ["hostile"])
    )
    assert_artifact_verify_rejects(
        lambda value: value["services"][internal_key]["spec"]["template"][
            "spec"
        ].__setitem__("volumes", [{"name": "hostile"}])
    )
    assert_artifact_verify_rejects(
        lambda value: internal_container(value)["resources"]["limits"].__setitem__(
            "memory", "4Gi"
        )
    )
    hostile_job_key = f"trusted-router-synthetic-{REGIONS[0]}|{REGIONS[0]}"
    assert_artifact_verify_rejects(
        lambda value: value["jobs"][hostile_job_key]["spec"]["template"]["spec"][
            "template"
        ]["spec"]["containers"][0].__setitem__(
            "image", IMAGE.rsplit(":", 1)[0] + ":" + "b" * 64
        )
    )

    def add_path_to_private_health_origin(value: dict[str, Any]) -> None:
        env = value["jobs"][hostile_job_key]["spec"]["template"]["spec"][
            "template"
        ]["spec"]["containers"][0]["env"]
        item = next(
            item
            for item in env
            if item["name"] == "TR_SYNTHETIC_CONTROL_PLANE_HEALTH_URL"
        )
        item["value"] += "/healthz"

    assert_artifact_verify_rejects(add_path_to_private_health_origin)
    hostile_scheduler_key = (
        f"trusted-router-synthetic-{REGIONS[0]}-every-three-minutes|{REGIONS[0]}"
    )

    def add_scheduler_execution_override(value: dict[str, Any]) -> None:
        target = value["schedulers"][hostile_scheduler_key]["httpTarget"]
        target["body"] = "eyJvdmVycmlkZSI6dHJ1ZX0="
        target["headers"] = {"X-TrustedRouter-Override": "true"}

    assert_artifact_verify_rejects(add_scheduler_execution_override)
    state_path.write_text(json.dumps(clean_verified_state), encoding="utf-8")

    # The artifact verifier is fail-closed and read-only when one job still
    # points at the legacy console origin.
    state = json.loads(state_path.read_text(encoding="utf-8"))
    broken_key = f"trusted-router-synthetic-{REGIONS[0]}|{REGIONS[0]}"
    broken_env = state["jobs"][broken_key]["spec"]["template"]["spec"][
        "template"
    ]["spec"]["containers"][0]["env"]
    next(
        item for item in broken_env if item["name"] == "TR_SYNTHETIC_INGEST_URL"
    )["value"] = (
        "https://trusted-router-123456789.us-central1.run.app/"
        "v1/internal/synthetic/samples"
    )
    state_path.write_text(json.dumps(state), encoding="utf-8")
    before_failed_verify = len(events_path.read_text(encoding="utf-8").splitlines())
    failed_verify = subprocess.run(  # noqa: S603
        [
            "/bin/bash",
            str(BOOTSTRAP),
            "--verify-artifact",
            str(bootstrap_artifact),
            "--expected-image",
            IMAGE,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert failed_verify.returncode != 0
    failed_verify_events = events_path.read_text(encoding="utf-8").splitlines()[
        before_failed_verify:
    ]
    assert not any(
        " run deploy " in f" {line} "
        or " update-traffic " in f" {line} "
        or " url-maps import " in f" {line} "
        for line in failed_verify_events
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["jobs"][broken_key] = _synthetic_job(
        REGIONS[0], f"trusted-router-synthetic-{REGIONS[0]}"
    )
    state["deploy_fail_after_remaining"] = 1
    state_path.write_text(json.dumps(state), encoding="utf-8")
    env["TR_INTERNAL_BOOTSTRAP_ARTIFACT"] = str(bootstrap_artifact)
    stage_journal_path = Path(f"{manifest_path}.stage.state")
    env["EXPECTED_STAGE_JOURNAL"] = str(stage_journal_path)
    main_event_start = len(events_path.read_text(encoding="utf-8").splitlines())
    result = subprocess.run(  # noqa: S603
        ["/bin/bash", str(ROLLOUT), "--manifest", str(manifest_path)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "deploy command exited non-zero; inspecting immutable postconditions" in result.stderr
    assert "no Cloud Run traffic or HTTPS URL-map route was changed" in result.stderr
    assert stage_journal_path.exists()
    assert stat.S_IMODE(stage_journal_path.stat().st_mode) == 0o600
    completed_stage = json.loads(stage_journal_path.read_text(encoding="utf-8"))
    assert completed_stage["phase"] == "complete"
    assert completed_stage["revision_suffix"] == "rstage1"
    assert {item["state"] for item in completed_stage["edge_states"]} == {
        "reconciled"
    }
    assert {item["state"] for item in completed_stage["service_states"]} == {
        "staged"
    }

    subprocess.run(  # noqa: S603
        [sys.executable, str(STATE_TOOL), "validate-manifest", str(manifest_path)],
        check=True,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prior_snapshot = manifest_path.parent / manifest["url_map"]["prior_snapshot"]
    candidate_snapshot = manifest_path.parent / manifest["url_map"]["candidate_snapshot"]
    assert prior_snapshot.exists() and candidate_snapshot.exists()
    assert stat.S_IMODE(manifest_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(prior_snapshot.stat().st_mode) == 0o600
    assert stat.S_IMODE(candidate_snapshot.stat().st_mode) == 0o600
    assert json.loads(prior_snapshot.read_text(encoding="utf-8")) != json.loads(
        candidate_snapshot.read_text(encoding="utf-8")
    )
    assert manifest["rollout_mode"] == "initial_split"
    assert manifest["bootstrap_artifact_sha256"] == __import__("hashlib").sha256(
        bootstrap_artifact.read_bytes()
    ).hexdigest()
    persisted_legacy = Path(f"{manifest_path}.legacy-hardening.json")
    persisted_frontend = Path(f"{manifest_path}.frontend-attestation.json")
    assert stat.S_IMODE(persisted_legacy.stat().st_mode) == 0o600
    assert stat.S_IMODE(persisted_frontend.stat().st_mode) == 0o600
    assert manifest["legacy_hardening_artifact_sha256"] == hashlib.sha256(
        persisted_legacy.read_bytes()
    ).hexdigest()
    assert manifest["frontend_attestation_sha256"] == hashlib.sha256(
        persisted_frontend.read_bytes()
    ).hexdigest()
    assert completed_stage["legacy_hardening_artifact_sha256"] == manifest[
        "legacy_hardening_artifact_sha256"
    ]
    assert completed_stage["frontend_attestation_sha256"] == manifest[
        "frontend_attestation_sha256"
    ]
    assert manifest["regions"] == list(REGIONS)
    assert manifest["internal_regions"] == list(REGIONS)
    assert manifest["primary_region"] == REGIONS[0]
    assert manifest["url_map"]["https_proxy"] == (
        "trusted-router-control-https-proxy"
    )
    assert len(manifest["services"]) == len(SURFACES) * len(REGIONS)
    assert {item["candidate_revision"] for item in manifest["services"]} == {
        *(f"{SERVICES[surface]}-rstage1" for surface in SURFACES if surface != "internal"),
        "trusted-router-billing-iboot1",
    }
    assert all(
        item["adopted_bootstrap"] == (item["surface"] == "internal")
        for item in manifest["services"]
    )

    final_state = json.loads(state_path.read_text(encoding="utf-8"))
    assert final_state["deploy_fail_after_remaining"] == 0
    assert len(final_state["deployments"]) == len(SURFACES) * len(REGIONS)
    for surface in SURFACES:
        for region in REGIONS:
            service = final_state["services"][f"{SERVICES[surface]}|{region}"]
            prior_revision = (
                "trusted-router-billing-iboot1"
                if surface == "internal"
                else f"{SERVICES[surface]}-rstage1"
            )
            assert service["status"]["latestReadyRevisionName"] == prior_revision
            assert service["status"]["traffic"] == [
                {"revisionName": prior_revision, "percent": 100}
            ]
    for region in REGIONS:
        legacy = final_state["services"][f"trusted-router|{region}"]
        assert legacy["status"]["traffic"] == [
            {"revisionName": "trusted-router-prior", "percent": 100}
        ]

    events = events_path.read_text(encoding="utf-8").splitlines()
    main_events = events[main_event_start:]
    first_mutation = next(
        index
        for index, line in enumerate(main_events)
        if " backend-services update " in f" {line} "
        or " security-policies rules update " in f" {line} "
        or " run deploy " in f" {line} "
    )
    synthetic_proof = [
        index
        for index, line in enumerate(main_events)
        if " scheduler jobs describe " in f" {line} "
    ]
    assert synthetic_proof and max(synthetic_proof) < first_mutation
    assert len([line for line in main_events if " run deploy " in f" {line} "]) == 10
    assert not any(
        "--no-traffic" in line
        for line in main_events
        if " run deploy " in f" {line} "
    )
    assert any("backend-services update trusted-router-public-backend" in line for line in main_events)
    assert any(
        "security-policies import trusted-router-public-edge" in line
        for line in main_events
    )
    assert any("network-endpoint-groups describe trusted-router-billing-neg" in line for line in main_events)
    assert not any("run services update-traffic" in line for line in main_events)
    assert not any("url-maps import" in line for line in events)
