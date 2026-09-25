#!/usr/bin/env python3
"""Offline validation/rendering for gateway_edge.sh; no cloud SDK dependency."""
from __future__ import annotations

import argparse
import json
import math
import re
from pathlib import Path
from typing import Any

if __package__:
    from .service_surface_url_map import (
        _MATCHER_NAME,
        _PATTERNS,
        existing_gateway_backend,
        rewrite_url_map,
    )
else:
    from service_surface_url_map import (
        _MATCHER_NAME,
        _PATTERNS,
        existing_gateway_backend,
        rewrite_url_map,
    )

# Conservative inventory, deliberately closed to unknown locations. Geographic
# distance is a preflight guard, not a guarantee of Google's network routing.
GEO = {
    "us-central1": (41.262, -95.860),
    "us-west1": (45.523, -122.676),
    "us-east4": (39.045, -77.487),
    "europe-west4": (52.379, 4.900),
    "southamerica-east1": (-23.550, -46.633),
    "us-east1": (33.836, -81.163),
}
ENCLAVES = ("us-central1", "us-west1", "us-east4", "europe-west4")
OUTLIER = {
    "consecutiveErrors": 5,
    "enforcingConsecutiveErrors": 100,
    "consecutiveGatewayFailure": 3,
    "enforcingConsecutiveGatewayFailure": 100,
    "interval": {"seconds": 1, "nanos": 0},
    "baseEjectionTime": {"seconds": 30, "nanos": 0},
    # With >=2 NEGs this allows failover but never ejects the whole pool.
    "maxEjectionPercent": 50,
}


def distance(a: str, b: str) -> float:
    lat1, lon1 = map(math.radians, GEO[a])
    lat2, lon2 = map(math.radians, GEO[b])
    return 2 * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    ))


def regions(primary: str, failovers: str, enclaves: str) -> list[str]:
    targets = [primary, *failovers.split(",")]
    origins = set(ENCLAVES) | set(enclaves.split(","))
    if len(targets) < 2 or len(set(targets)) != len(targets) or "" in targets:
        raise ValueError("at least two distinct gateway regions are required")
    if (set(targets) | origins) - GEO.keys():
        raise ValueError("unknown gateway/enclave geography; review GEO before proceeding")
    for origin in sorted(origins):
        for failover in targets[1:]:
            if distance(origin, failover) <= distance(origin, primary):
                raise ValueError(f"{failover} is not farther than {primary} from enclave {origin}")
    return targets


def backend(project: str, name: str, targets: list[str]) -> dict[str, Any]:
    base = f"https://www.googleapis.com/compute/v1/projects/{project}"
    return {
        "name": name,
        "loadBalancingScheme": "EXTERNAL_MANAGED",
        "protocol": "HTTP",
        "enableCDN": False,
        "timeoutSec": 30,
        "customRequestHeaders": ["X-TrustedRouter-Client-IP:{client_ip_address}"],
        "logConfig": {"enable": True, "sampleRate": 0.1},
        "securityPolicy": f"{base}/global/securityPolicies/trusted-router-legacy-edge",
        "backends": [{"group": f"{base}/regions/{region}/networkEndpointGroups/"
                     "trusted-router-control-neg"} for region in targets],
        "outlierDetection": OUTLIER,
    }


def verify_backend(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    for key, value in expected.items():
        if key == "backends":
            groups = [item.get("group") for item in actual.get(key, [])]
            if sorted(groups) != sorted(item["group"] for item in value):
                raise ValueError("gateway backend NEG membership differs")
            if any(item.get("preference", "DEFAULT") != "DEFAULT"
                   for item in actual.get(key, [])):
                raise ValueError("serverless backend preference is forbidden")
        elif key == "outlierDetection":
            observed = actual.get(key, {})
            for field, wanted in value.items():
                got = observed.get(field)
                if isinstance(wanted, dict):
                    got = {"seconds": int((got or {}).get("seconds", 0)),
                           "nanos": int((got or {}).get("nanos", 0))}
                if got != wanted:
                    raise ValueError(f"gateway outlierDetection.{field} differs")
        elif actual.get(key) != value:
            raise ValueError(f"gateway backend {key} differs")
    if actual.get("healthChecks") or actual.get("iap", {}).get("enabled"):
        raise ValueError("serverless billing backend must not have health checks or IAP")


def active_revision(service: dict[str, Any]) -> str:
    status = service.get("status", {})
    if not any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in status.get("conditions", [])):
        raise ValueError("gateway service is not Ready")
    traffic = [t for t in status.get("traffic", []) if t.get("percent", 0) > 0]
    if len(traffic) != 1 or traffic[0].get("percent") != 100:
        raise ValueError("gateway service must have one explicit 100% serving revision")
    revision = traffic[0].get("revisionName", "")
    if not re.fullmatch(r"[a-z][a-z0-9-]*", revision):
        raise ValueError("missing or invalid serving revision name")
    annotations = service.get("metadata", {}).get("annotations", {})
    if int(annotations.get("run.googleapis.com/minScale", 0)) < 1:
        raise ValueError("gateway service requires service-level min instances >= 1; run rollout")
    if annotations.get("run.googleapis.com/ingress") != "internal-and-cloud-load-balancing":
        raise ValueError("gateway service must use internal-and-cloud-load-balancing ingress")
    return str(revision)


def release(revision: dict[str, Any]) -> tuple[str, str]:
    status = revision.get("status", {})
    if not any(c.get("type") == "Ready" and c.get("status") == "True"
               for c in status.get("conditions", [])):
        raise ValueError("serving revision is not Ready")
    containers = revision.get("spec", {}).get("containers", [])
    if len(containers) != 1:
        raise ValueError("expected one gateway container")
    env = {item["name"]: item.get("value") for item in containers[0].get("env", [])}
    value = env.get("TR_RELEASE", "") or ""
    digest = status.get("imageDigest", "").rsplit("@", 1)[-1]
    if not re.fullmatch(r"[0-9a-f]{7,40}", value):
        raise ValueError("serving revision lacks a commit TR_RELEASE")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("serving revision lacks a resolved image digest")
    if env.get("TR_SERVICE_SURFACE", "combined") not in {"combined", "internal"}:
        raise ValueError("serving revision does not mount gateway handlers")
    return value, digest


def verify_fleet(state: Path, targets: list[str], project: str) -> None:
    releases = []
    for region in targets:
        service = json.loads((state / f"{region}.service.json").read_text())
        name = active_revision(service)
        revision = json.loads((state / f"{region}.revision.json").read_text())
        if revision.get("metadata", {}).get("name") != name:
            raise ValueError("revision evidence does not match serving traffic")
        releases.append(release(revision))
        neg = json.loads((state / f"{region}.neg.json").read_text())
        expected_link = (f"https://www.googleapis.com/compute/v1/projects/{project}/regions/"
                         f"{region}/networkEndpointGroups/trusted-router-control-neg")
        if (neg.get("networkEndpointType") != "SERVERLESS"
                or neg.get("cloudRun") != {"service": "trusted-router"}
                or neg.get("selfLink") != expected_link):
            raise ValueError(f"wrong serverless control NEG in {region}")
    if len(set(releases)) != 1:
        raise ValueError("gateway regions run different releases/digests")


def candidate(live: dict[str, Any], gateway: str, domains: list[str]) -> dict[str, Any]:
    matchers = [m for m in live.get("pathMatchers", []) if m.get("name") == _MATCHER_NAME]
    if len(matchers) != 1:
        raise ValueError("requires existing service-surface matcher; run public cutover first")
    gateway = str(gateway)
    previous_gateway = existing_gateway_backend(live)
    if len(matchers[0].get("pathRules", [])) != 4 + bool(previous_gateway):
        raise ValueError("unrecognized live path rules; refusing to discard them")
    links = []
    for patterns in _PATTERNS.values():
        matches = [r.get("service") for r in matchers[0].get("pathRules", [])
                   if set(r.get("paths", [])) == set(patterns)]
        if len(matches) != 1 or not matches[0]:
            raise ValueError("ambiguous existing surface assignment")
        links.append(matches[0])
    return rewrite_url_map(live, *links, domains, gateway_backend=gateway)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("regions", "backend", "verify-backend", "revision",
                                            "fleet", "candidate"))
    parser.add_argument("--primary", default="us-central1")
    parser.add_argument("--failovers", default="southamerica-east1")
    parser.add_argument("--enclaves", default=",".join(ENCLAVES))
    parser.add_argument("--project", default="quill-cloud-proxy")
    parser.add_argument("--backend", default="trusted-router-gateway-backend")
    parser.add_argument("--state", type=Path)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--domains", default="trustedrouter.com,allyrouter.com,uptimerouter.com")
    args = parser.parse_args()
    targets = regions(args.primary, args.failovers, args.enclaves)
    desired = backend(args.project, args.backend, targets)
    if args.command == "regions":
        print(",".join(targets))
    elif args.command == "backend":
        print(json.dumps(desired, indent=2))
    elif args.command == "verify-backend":
        verify_backend(json.loads(args.input.read_text()), desired)
    elif args.command == "revision":
        print(active_revision(json.loads(args.input.read_text())))
    elif args.command == "fleet":
        verify_fleet(args.state, targets, args.project)
    elif args.command == "candidate":
        link = (f"https://www.googleapis.com/compute/v1/projects/{args.project}/global/"
                f"backendServices/{args.backend}")
        print(json.dumps(candidate(json.loads(args.input.read_text()), link,
                                   args.domains.split(",")), indent=2))


if __name__ == "__main__":
    main()
