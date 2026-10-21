#!/usr/bin/env python3
"""Clone a live ECS task definition with its new image and release marker.

It also pins TR_REMEDIATOR_IN_PROCESS_ENABLED=false: on AWS the EventBridge tick
(infra/aws_synthetic_monitoring.tf) is the only remediator scheduler.
The regional outbox expectation can enable a previously disabled producer.
The record-read expectation selects ClickHouse with reader-only credentials.
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


def prepare(
    payload: dict, image: str, release: str, required_outbox: str = "true",
    required_record_read_backend: str = "postgres", reader_secret_arn: str = "",
) -> dict:
    if required_outbox not in ("true", "false"):
        raise ValueError("invalid required TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED")
    if required_record_read_backend not in ("postgres", "clickhouse"):
        raise ValueError("invalid required TR_RECORD_READ_BACKEND")
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
    # Both regions write the shared DSQL outbox; only the drain needs ClickHouse
    # credentials. Permit the false -> true rollout, never the reverse or an
    # absent/invalid live setting.
    live_outbox = values.get("TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED")
    if live_outbox != required_outbox and (live_outbox, required_outbox) != ("false", "true"):
        raise ValueError("live TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED differs from regional expectation")
    # The in-process remediator loop defaults on. A live definition without the
    # setting gets it added as false; any value other than false is refused.
    in_process = values.get("TR_REMEDIATOR_IN_PROCESS_ENABLED")
    if in_process not in (None, "false"):
        raise ValueError("TR_REMEDIATOR_IN_PROCESS_ENABLED must be false on the AWS observer")
    # The regional expectation decides, in either direction: Postgres is
    # written in both modes, so moving back to it loses nothing. Only a live
    # value outside the two is refused.
    if values.get("TR_RECORD_READ_BACKEND") not in (None, "postgres", "clickhouse"):
        raise ValueError("invalid live TR_RECORD_READ_BACKEND")
    updates = {"TR_RECORD_READ_BACKEND": required_record_read_backend}
    if required_record_read_backend == "clickhouse":
        if not values.get("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL", "").strip():
            raise ValueError("clickhouse record reads require a cloned TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL")
        # The standalone nodes keep their tables in `default`, and the reader
        # is granted `default.*` only; any other database answers every read
        # with an access error.
        if values.get("TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE") != "default":
            raise ValueError("clickhouse record reads require a cloned TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=default")
        if not re.fullmatch(r"arn:aws:secretsmanager:[a-z0-9-]+:[0-9]{12}:secret:[A-Za-z0-9/_+=.@-]+", reader_secret_arn):
            raise ValueError("clickhouse record reads require a reader secret ARN")
        credential_name = "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD"
        secrets = container.setdefault("secrets", [])
        names = [secret["name"] for secret in secrets]
        if len(set(names)) != len(names):
            raise ValueError("duplicate secret references")
        if credential_name in values or "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER" in names or "TR_RECORD_READ_BACKEND" in names:
            raise ValueError("conflicting ClickHouse record-read environment and secret settings")
        updates["TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER"] = "tr_control_read"
        if credential_name in names:
            secrets[names.index(credential_name)]["valueFrom"] = reader_secret_arn
        else:
            secrets.append({"name": credential_name, "valueFrom": reader_secret_arn})
    container["image"] = image
    for entry in env:
        if entry["name"] in {"TR_RELEASE", "RELEASE_COMMIT"}:
            entry["value"] = release
        elif entry["name"] == "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED":
            entry["value"] = required_outbox
        elif entry["name"] in updates:
            entry["value"] = updates[entry["name"]]
    if in_process is None:
        env.append({"name": "TR_REMEDIATOR_IN_PROCESS_ENABLED", "value": "false"})
    for name, value in updates.items():
        if name not in values:
            env.append({"name": name, "value": value})
    return result


if __name__ == "__main__":
    try:
        with open(sys.argv[1], encoding="utf-8") as source:
            result = prepare(
                json.load(source), sys.argv[2], sys.argv[3], sys.argv[5], sys.argv[6], sys.argv[7],
            )
        with open(sys.argv[4], "w", encoding="utf-8") as destination:
            json.dump(result, destination)
    except ValueError as reason:
        # prepare()'s own refusals name settings, never their values, so the
        # reason is safe to print and makes the next refusal explain itself.
        raise SystemExit(
            f"Cannot safely clone the ECS workload; no runtime configuration printed ({reason})"
        ) from None
    except (OSError, KeyError, TypeError, IndexError):
        raise SystemExit("Cannot safely clone the ECS workload; no runtime configuration printed") from None
