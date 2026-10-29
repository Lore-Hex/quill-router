"""Allowlist the dedicated worker from a verified serving router revision."""
from __future__ import annotations

import json
import re
import sys
from typing import Any

PINS = {
    "TR_ENVIRONMENT": "production",
    "TR_SERVICE_SURFACE": "settle-drain",
    "TR_PRIMARY_REGION": "us-central1",
    "TR_STORAGE_BACKEND": "spanner-clickhouse",
    "TR_SPANNER_POOL_SIZE": "8",
    "TR_ASYNC_SETTLE_PROTECTION": "true",
    "TR_ASYNC_SETTLE_ENABLED": "false",
    "TR_SETTLE_OUTBOX_ENABLED": "true",
    # Config-as-code off switch: use "false" to deploy an idle, healthy worker.
    "TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED": "true",
    "TR_SETTLE_OUTBOX_POLL_INTERVAL_SECONDS": "1",
    "TR_SETTLE_OUTBOX_PASS_BUDGET_SECONDS": "2",
    "TR_SETTLE_OUTBOX_LEASE_SECONDS": "30",
    "TR_SETTLE_OUTBOX_STALL_SECONDS": "26",
    "TR_SETTLE_OUTBOX_WORKER_CONCURRENCY": "4",
    "TR_SETTLE_OUTBOX_CLAIM_BATCH": "4",
    "TR_SETTLE_OUTBOX_HEALTH_PUBLISH_INTERVAL_SECONDS": "1",
    "TR_GENERATION_RECORDS_ENABLED": "true",
    "TR_REQUEST_RECORD_WRITE_MODE": "typed",
    "TR_ANALYTICS_OUTBOX_ENABLED": "true",
    "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED": "true",
    "TR_OPERATIONAL_ANALYTICS_SINK": "outbox",
    "TR_SHUTDOWN_DRAIN_SECONDS": "0",
    "TR_LOOP_STALL_EXIT_SECONDS": "30",
}
COPIED = ("TR_GCP_PROJECT_ID", "TR_SPANNER_INSTANCE_ID", "TR_SPANNER_DATABASE_ID",
          "TR_RELEASE", "TR_SOURCE_REVISION")


def worker_plan(revision: dict[str, Any]) -> dict[str, Any]:
    spec = revision["spec"]
    [container] = spec["containers"]
    image = container["image"]
    if not re.fullmatch(r"[^\s]+@sha256:[0-9a-f]{64}", image):
        raise ValueError("serving router image must be digest pinned")
    service_account = spec["serviceAccountName"]
    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.iam\.gserviceaccount\.com", service_account):
        raise ValueError("explicit router service account required")
    pairs = container.get("env", [])
    if len({p["name"] for p in pairs}) != len(pairs):
        raise ValueError("ambiguous router environment")
    source = {p["name"]: p.get("value") for p in pairs}
    if source.get("TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED") != "false":
        raise ValueError("router fast drain must remain disabled")
    env = dict(PINS)
    for name in COPIED:
        value = source.get(name)
        if not isinstance(value, str) or not value or any(c in value for c in "|\n\r"):
            raise ValueError(f"missing/unsafe plain setting: {name}")
        env[name] = value
    if not re.fullmatch(r"[0-9a-f]{40}", env["TR_SOURCE_REVISION"]):
        raise ValueError("full released source revision required")
    if not re.fullmatch(r"[0-9a-f]{7,40}", env["TR_RELEASE"]) or not env["TR_SOURCE_REVISION"].startswith(env["TR_RELEASE"]):
        raise ValueError("release does not match source revision")
    # Only the existing paging destination is inherited. Never resolve or print
    # its value; Cloud Run mounts it through the same service account.
    [sentry] = [p for p in pairs if p["name"] == "TR_SENTRY_DSN"]
    reference = sentry.get("valueFrom", {}).get("secretKeyRef", {})
    if (set(reference) != {"name", "key"}
            or not re.fullmatch(r"[A-Za-z0-9_-]+", reference["name"])
            or not re.fullmatch(r"[0-9]+|latest", reference["key"])):
        raise ValueError("router Sentry Secret Manager reference required")
    return {"image": image, "service_account": service_account, "env": env,
            "secrets": {"TR_SENTRY_DSN": reference}}


if __name__ == "__main__":
    print(json.dumps(worker_plan(json.load(sys.stdin)), sort_keys=True))
