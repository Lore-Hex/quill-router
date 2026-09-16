"""Reconcile dedicated funding alerts without creating or weakening channels."""

from __future__ import annotations

import argparse
import json
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.lightning.activate_web import PROJECT, WEB, Operator

HEARTBEAT_METRIC = "lightning_funding_worker_heartbeat"
LIQUIDITY_METRIC = "lightning_funding_liquidity_heartbeat"
SURFACE = f'resource.type="cloud_run_revision" AND resource.labels.service_name="{WEB}"'
HEARTBEAT = (
    SURFACE
    + ' AND (jsonPayload.event="lightning.funding_health" OR textPayload:"lightning.funding_health")'
)
LIQUIDITY_HEARTBEAT = (
    SURFACE
    + ' AND (jsonPayload.event="lightning.liquidity_health" OR textPayload:"lightning.liquidity_health")'
)


def policies(channel: str) -> list[dict[str, Any]]:
    definitions = [
        (
            "LightningRouter: payment needs review",
            SURFACE
            + ' AND ((jsonPayload.event="lightning.funding_health" AND (jsonPayload.review_required>0 OR jsonPayload.oldest_uncredited_seconds>=120)) OR textPayload:"lightning.funding_stalled")',
        ),
        (
            "LightningRouter: reconciliation worker failed",
            SURFACE + ' AND textPayload:"lightning.worker_failed"',
        ),
    ]
    result = [
        {
            "displayName": name,
            "combiner": "OR",
            "enabled": True,
            "notificationChannels": [channel],
            "documentation": {
                "mimeType": "text/markdown",
                "content": "Inspect the isolated funding outbox. Preserve all paid/unresolved rows. Never fabricate settlement or discard a failed credit. Runbook: docs/lightning-funding-production.md.",
            },
            "conditions": [{"displayName": name, "conditionMatchedLog": {"filter": query}}],
            "alertStrategy": {"notificationRateLimit": {"period": "300s"}, "autoClose": "1800s"},
        }
        for name, query in definitions
    ]
    result.append(
        {
            "displayName": "LightningRouter: reconciliation heartbeat missing",
            "combiner": "OR",
            "enabled": True,
            "notificationChannels": [channel],
            "conditions": [
                {
                    "displayName": "No funding heartbeat for 10 minutes",
                    "conditionAbsent": {
                        "filter": f'metric.type="logging.googleapis.com/user/{HEARTBEAT_METRIC}" AND resource.type="cloud_run_revision" AND resource.label.service_name="{WEB}"',
                        "duration": "600s",
                        "aggregations": [
                            {
                                "alignmentPeriod": "60s",
                                "perSeriesAligner": "ALIGN_RATE",
                                "crossSeriesReducer": "REDUCE_SUM",
                                "groupByFields": ["resource.label.service_name"],
                            }
                        ],
                        "trigger": {"count": 1},
                    },
                }
            ],
        }
    )
    result.append({
        "displayName": "LightningRouter: inbound liquidity needs attention",
        "combiner": "OR", "enabled": True, "notificationChannels": [channel],
        "documentation": {"mimeType": "text/markdown", "content":
            "Receiving capacity is below 300,000 sats or its check failed. Inspect LND sync, active channels, reserves and in-flight limits. Do not increase spending authority or reset Loop budgets. Replenishment requires the approved capped policy. Runbook: docs/lightning-liquidity.md."},
        "conditions": [{"displayName": "Low or unknown inbound capacity", "conditionMatchedLog": {
            "filter": SURFACE + ' AND (textPayload:"lightning.liquidity_low" OR textPayload:"lightning.liquidity_check_failed")'}}],
        "alertStrategy": {"notificationRateLimit": {"period": "3600s"}, "autoClose": "1800s"},
    })
    result.append({
        "displayName": "LightningRouter: liquidity heartbeat missing",
        "combiner": "OR", "enabled": True, "notificationChannels": [channel],
        "conditions": [{"displayName": "No liquidity sample for 10 minutes", "conditionAbsent": {
            "filter": f'metric.type="logging.googleapis.com/user/{LIQUIDITY_METRIC}" AND resource.type="cloud_run_revision" AND resource.label.service_name="{WEB}"',
            "duration": "600s", "aggregations": [{"alignmentPeriod": "60s", "perSeriesAligner": "ALIGN_RATE",
                "crossSeriesReducer": "REDUCE_SUM", "groupByFields": ["resource.label.service_name"]}], "trigger": {"count": 1}}}],
    })
    return result


def install(operator: Operator) -> None:
    channels = json.loads(operator.gc("beta", "monitoring", "channels", "list", "--format=json"))
    channels = [
        c
        for c in channels
        if c.get("displayName") == "TrustedRouter Spanner on-call"
        and c.get("enabled") is True
        and c.get("verificationStatus", "VERIFICATION_STATUS_UNSPECIFIED")
        in {"VERIFIED", "VERIFICATION_STATUS_UNSPECIFIED"}
    ]
    if len(channels) != 1:
        raise ValueError("Exactly one existing verified on-call notification channel is required")
    metrics = operator.gc("logging", "metrics", "list", "--format=value(name)").splitlines()
    for name, query in ((HEARTBEAT_METRIC, HEARTBEAT), (LIQUIDITY_METRIC, LIQUIDITY_HEARTBEAT)):
        operator.gc("logging", "metrics", "update" if name in metrics else "create", name,
                    "--description=Lightning funding monitoring heartbeat", "--log-filter=" + query)
    # An absence policy cannot detect a metric that never existed. Fail the
    # installation unless the live worker emits after metric creation.
    since = datetime.now(UTC).isoformat()
    for query in (HEARTBEAT, LIQUIDITY_HEARTBEAT):
        for _ in range(12):
            heartbeat = operator.gc("logging", "read", query + f' AND timestamp>"{since}"',
                                    "--limit=1", "--format=value(timestamp)")
            if heartbeat.strip():
                break
            time.sleep(15)
        else:
            raise RuntimeError("No live funding heartbeat; alert installation is not complete")
    current = json.loads(operator.gc("monitoring", "policies", "list", "--format=json"))
    for policy in policies(channels[0]["name"]):
        matches = [p for p in current if p.get("displayName") == policy["displayName"]]
        if len(matches) > 1:
            raise ValueError("Ambiguous funding alert policy")
        with tempfile.TemporaryDirectory(prefix="lightning-alert-") as directory:
            path = Path(directory) / "policy.json"
            path.write_text(json.dumps(policy))
            args = ("update", matches[0]["name"]) if matches else ("create",)
            operator.gc("monitoring", "policies", *args, "--policy-from-file=" + str(path))
        print("Configured " + policy["displayName"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply:
        install(Operator(args.account))
    else:
        print(json.dumps(policies(f"projects/{PROJECT}/notificationChannels/DRY_RUN"), indent=2))


if __name__ == "__main__":
    main()
