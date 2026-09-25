#!/usr/bin/env python3
"""Clone a live ECS task definition, changing only its image and release marker.

It also pins TR_REMEDIATOR_IN_PROCESS_ENABLED=false: on AWS the EventBridge tick
(infra/aws_synthetic_monitoring.tf) is the only remediator scheduler.
"""

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
    if required_outbox not in ("true", "false"):
        raise ValueError("invalid required TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("immutable image required")
    if not re.fullmatch(r"[0-9a-f]{40}", release):
        raise ValueError("full commit SHA required")
    definition = payload["taskDefinition"]
    if set(definition) - REGISTER_FIELDS - RESPONSE_FIELDS:
        raise ValueError("unrecognized task-definition fields; review before deploying")
    result = copy.deepcopy({k: v for k, v in definition.items() if k in REGISTER_FIELDS})
    # RegisterTaskDefinition rejects an empty list ("Tags can not be empty"), and
    # describe-task-definition returns none for an untagged definition.
    tags = copy.deepcopy(payload.get("tags") or [])
    if tags:
        result["tags"] = tags
    containers = result["containerDefinitions"]
    if len(containers) != 1 or containers[0]["name"] != "trusted-router":
        raise ValueError("unexpected ECS workload")
    container = containers[0]
    env = container.get("environment", [])
    values = {e["name"]: e["value"] for e in env}
    if len(values) != len(env):
        raise ValueError("duplicate environment variables")
    if values.get("TR_SERVICE_SURFACE") != "observer" or not values.get("TR_RELEASE"):
        raise ValueError("expected live observer with release marker")
    if values.get("TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED") != required_outbox:
        raise ValueError("live TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED differs from regional expectation")
    has_clickhouse_secret = any(
        secret["name"] == "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD"
        for secret in container.get("secrets", [])
    )
    if (values.get("TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED") == "true") != has_clickhouse_secret:
        raise ValueError("TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED conflicts with TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD")
    # The in-process remediator loop defaults on. A live definition without the
    # setting gets it added as false; any value other than false is refused.
    in_process = values.get("TR_REMEDIATOR_IN_PROCESS_ENABLED")
    if in_process not in (None, "false"):
        raise ValueError("TR_REMEDIATOR_IN_PROCESS_ENABLED must be false on the AWS observer")
    container["image"] = image
    for entry in env:
        if entry["name"] in {"TR_RELEASE", "RELEASE_COMMIT"}:
            entry["value"] = release
    if in_process is None:
        env.append({"name": "TR_REMEDIATOR_IN_PROCESS_ENABLED", "value": "false"})
    return result


if __name__ == "__main__":
    try:
        with open(sys.argv[1], encoding="utf-8") as source:
            result = prepare(json.load(source), sys.argv[2], sys.argv[3], sys.argv[5])
        with open(sys.argv[4], "w", encoding="utf-8") as destination:
            json.dump(result, destination)
    except ValueError as reason:
        # prepare()'s own refusals name settings, never their values, so the
        # reason is safe to print and makes the next refusal explain itself.
        raise SystemExit(
            f"Cannot safely clone the ECS workload; no runtime configuration printed ({reason})"
        ) from None
    except (OSError, KeyError, TypeError):
        raise SystemExit("Cannot safely clone the ECS workload; no runtime configuration printed") from None
