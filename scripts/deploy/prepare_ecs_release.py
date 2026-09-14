#!/usr/bin/env python3
"""Clone a live ECS task definition, changing only its image and release marker."""

from __future__ import annotations

import copy
import json
import re
import sys

REGISTER_FIELDS = frozenset({
    "family", "taskRoleArn", "executionRoleArn", "networkMode", "containerDefinitions",
    "volumes", "placementConstraints", "requiresCompatibilities", "cpu", "memory",
    "pidMode", "ipcMode", "proxyConfiguration", "inferenceAccelerators",
    "ephemeralStorage", "runtimePlatform", "enableFaultInjection",
})
RESPONSE_FIELDS = frozenset({
    "taskDefinitionArn", "revision", "status", "requiresAttributes", "compatibilities",
    "registeredAt", "registeredBy", "deregisteredAt",
})


def prepare(payload: dict, image: str, release: str, required_outbox: str = "true") -> dict:
    if required_outbox != "true":
        raise ValueError("analytics must remain enabled")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("immutable image required")
    if not re.fullmatch(r"[0-9a-f]{40}", release):
        raise ValueError("full commit SHA required")
    definition = payload["taskDefinition"]
    if set(definition) - REGISTER_FIELDS - RESPONSE_FIELDS:
        raise ValueError("unrecognized task-definition fields; review before deploying")
    result = copy.deepcopy({k: v for k, v in definition.items() if k in REGISTER_FIELDS})
    result["tags"] = copy.deepcopy(payload.get("tags", []))
    containers = result["containerDefinitions"]
    if len(containers) != 1 or containers[0]["name"] != "trusted-router":
        raise ValueError("unexpected ECS workload")
    container = containers[0]
    env = container.get("environment", [])
    values = {e["name"]: e["value"] for e in env}
    if len(values) != len(env):
        raise ValueError("duplicate environment variables")
    if (values.get("TR_SERVICE_SURFACE") != "observer"
            or values.get("TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED") != required_outbox
            or not values.get("TR_RELEASE")):
        raise ValueError("expected live observer with analytics and release marker")
    container["image"] = image
    for entry in env:
        if entry["name"] in {"TR_RELEASE", "RELEASE_COMMIT"}:
            entry["value"] = release
    return result


if __name__ == "__main__":
    try:
        with open(sys.argv[1], encoding="utf-8") as source:
            result = prepare(json.load(source), sys.argv[2], sys.argv[3], sys.argv[5])
        with open(sys.argv[4], "w", encoding="utf-8") as destination:
            json.dump(result, destination)
    except (OSError, KeyError, TypeError, ValueError):
        raise SystemExit("Cannot safely clone the ECS workload; no runtime configuration printed") from None
