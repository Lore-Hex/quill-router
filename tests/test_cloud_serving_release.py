from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash") or "/bin/bash"
SHA = "a" * 40
DIGEST = "sha256:" + "b" * 64
IMAGE = "registry/trusted-router@" + DIGEST
AWS_TARGETS = (("eu-west-1", "tr-cp-euw1"), ("eu-west-3", "tr-cp-euw3"))
STUB = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
key = " ".join(args[:2]) if sys.argv[0].endswith("aws") else " ".join(args[:3])
if "--region" in args:
    key += ":" + args[args.index("--region") + 1]
payload = json.load(open(os.environ["RELEASE_FIXTURES"]))
if key not in payload:
    sys.exit(1)
print(json.dumps(payload[key]))
'''


def aws_payloads() -> dict:
    payloads = {}
    for region, service in AWS_TARGETS:
        task_definition = f"arn:task-definition/{service}:19"
        payloads.update({
            f"ecs describe-services:{region}": {
                "failures": [],
                "services": [{
                    "status": "ACTIVE", "desiredCount": 1, "runningCount": 1,
                    "pendingCount": 0, "taskDefinition": task_definition,
                    "deployments": [{
                        "status": "PRIMARY", "rolloutState": "COMPLETED",
                        "taskDefinition": task_definition,
                    }],
                    "loadBalancers": [{
                        "targetGroupArn": "arn:target-group/test",
                        "containerName": "trusted-router", "containerPort": 8080,
                    }],
                }],
            },
            f"ecs describe-task-definition:{region}": {"taskDefinition": {
                "containerDefinitions": [{
                    "name": "trusted-router", "image": IMAGE,
                    "environment": [{"name": "TR_RELEASE", "value": SHA}],
                }],
            }},
            f"ecs list-tasks:{region}": {"taskArns": ["arn:task/test"]},
            f"ecs describe-tasks:{region}": {"failures": [], "tasks": [{
                "taskArn": "arn:task/test", "taskDefinitionArn": task_definition,
                "lastStatus": "RUNNING", "containers": [{
                    "name": "trusted-router", "lastStatus": "RUNNING",
                    "image": IMAGE, "imageDigest": DIGEST,
                    "networkInterfaces": [{"privateIpv4Address": "10.0.0.10"}],
                }],
            }]},
            f"elbv2 describe-target-health:{region}": {"TargetHealthDescriptions": [{
                "Target": {"Id": "10.0.0.10", "Port": 8080},
                "TargetHealth": {"State": "healthy"},
            }]},
        })
    return payloads


def discover(tmp_path: Path, cloud: str, payload: dict) -> subprocess.CompletedProcess:
    fixtures = tmp_path / "fixtures.json"
    fixtures.write_text(json.dumps(payload))
    for name in ("aws", "gcloud"):
        stub = tmp_path / name
        stub.write_text(STUB)
        stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TR_CLOUD_BAKE_")}
    return subprocess.run(  # noqa: S603 - fixed local gate and stubbed cloud CLIs
        [BASH, "-c", 'source "$1"; _cloud_bake_serving_tag "$2"', "test",
         str(ROOT / "scripts/deploy/cloud_bake_gate.sh"), cloud],
        env={**env, "PATH": f"{tmp_path}:{env['PATH']}", "RELEASE_FIXTURES": str(fixtures)},
        capture_output=True, text=True, check=False,
    )


def gcp_payload(image: str = IMAGE, release: str = SHA) -> dict:
    return {
        "run services describe:us-central1": {"status": {"traffic": [
            {"revisionName": "serving-revision", "percent": 100},
        ]}},
        "run revisions describe:us-central1": {"spec": {"containers": [{
            "image": image, "env": [{"name": "TR_RELEASE", "value": release}],
        }]}},
    }


def test_gcp_digest_pinned_serving_revision_is_known(tmp_path: Path) -> None:
    result = discover(tmp_path, "gcp", gcp_payload())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == SHA


@pytest.mark.parametrize("image,release", [
    (IMAGE, ""), (IMAGE, "main"), ("registry:latest", SHA),
    ("registry@sha256:broken", SHA), ("registry:" + "c" * 40, SHA),
])
def test_gcp_missing_or_conflicting_release_fails_closed(
    tmp_path: Path, image: str, release: str,
) -> None:
    result = discover(tmp_path, "gcp", gcp_payload(image, release))
    assert result.returncode != 0
    assert result.stdout == ""


def test_aws_uses_both_live_ecs_regions_not_retired_apprunner(tmp_path: Path) -> None:
    result = discover(tmp_path, "aws", aws_payloads())
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == SHA


def test_aws_draining_target_after_a_rollout_is_not_serving_evidence(tmp_path: Path) -> None:
    # After a rollout the deregistered target stays listed as "draining" for the
    # deregistration delay. It must not make the healthy fleet unreadable.
    payload = aws_payloads()
    for region in ("eu-west-1", "eu-west-3"):
        targets = payload[f"elbv2 describe-target-health:{region}"]["TargetHealthDescriptions"]
        old = copy.deepcopy(targets[0])
        old["Target"]["Id"] = "10.9.9.9"
        old["TargetHealth"] = {"State": "draining", "Reason": "Target.DeregistrationInProgress"}
        targets.append(old)
    result = discover(tmp_path, "aws", payload)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == SHA


@pytest.mark.parametrize("state", ["initial", "unhealthy", "unused", "unavailable"])
def test_aws_extra_target_that_is_not_draining_still_fails_closed(tmp_path: Path, state: str) -> None:
    payload = aws_payloads()
    targets = payload["elbv2 describe-target-health:eu-west-3"]["TargetHealthDescriptions"]
    extra = copy.deepcopy(targets[0])
    extra["Target"]["Id"] = "10.9.9.9"
    extra["TargetHealth"] = {"State": state}
    targets.append(extra)
    result = discover(tmp_path, "aws", payload)
    assert result.returncode != 0, result.stdout
    assert result.stdout == ""


def test_aws_only_a_draining_target_is_not_a_serving_fleet(tmp_path: Path) -> None:
    payload = aws_payloads()
    targets = payload["elbv2 describe-target-health:eu-west-3"]["TargetHealthDescriptions"]
    targets[0]["TargetHealth"] = {"State": "draining"}
    result = discover(tmp_path, "aws", payload)
    assert result.returncode != 0, result.stdout
    assert result.stdout == ""


@pytest.mark.parametrize("fault", [
    "missing_region", "service_failure", "zero_tasks", "pending", "rolling",
    "two_deployments", "missing_task", "task_failure", "old_task_definition",
    "stopped_container", "digest_mismatch", "release_mismatch", "no_release",
    "unhealthy_target", "wrong_target", "extra_target", "wrong_port",
])
def test_aws_incomplete_or_mixed_serving_evidence_fails_closed(tmp_path: Path, fault: str) -> None:
    payload = aws_payloads()
    region = "eu-west-3"
    service = payload[f"ecs describe-services:{region}"]["services"][0]
    task_data = payload[f"ecs describe-tasks:{region}"]
    task = task_data["tasks"][0]
    definition = payload[f"ecs describe-task-definition:{region}"]["taskDefinition"]
    target_data = payload[f"elbv2 describe-target-health:{region}"]
    target = target_data["TargetHealthDescriptions"][0]
    if fault == "missing_region":
        del payload[f"ecs describe-services:{region}"]
    elif fault == "service_failure":
        payload[f"ecs describe-services:{region}"]["failures"] = [{"reason": "MISSING"}]
    elif fault == "zero_tasks":
        service["desiredCount"] = service["runningCount"] = 0
    elif fault == "pending":
        service["pendingCount"] = 1
    elif fault == "rolling":
        service["deployments"][0]["rolloutState"] = "IN_PROGRESS"
    elif fault == "two_deployments":
        service["deployments"].append(copy.deepcopy(service["deployments"][0]))
    elif fault == "missing_task":
        task_data["tasks"] = []
    elif fault == "task_failure":
        task_data["failures"] = [{"reason": "MISSING"}]
    elif fault == "old_task_definition":
        task["taskDefinitionArn"] = "arn:task-definition/old:1"
    elif fault == "stopped_container":
        task["containers"][0]["lastStatus"] = "STOPPED"
    elif fault == "digest_mismatch":
        task["containers"][0]["imageDigest"] = "sha256:" + "c" * 64
    elif fault == "release_mismatch":
        definition["containerDefinitions"][0]["environment"][0]["value"] = "c" * 40
    elif fault == "no_release":
        definition["containerDefinitions"][0]["environment"] = []
    elif fault == "unhealthy_target":
        target["TargetHealth"]["State"] = "unhealthy"
    elif fault == "wrong_target":
        target["Target"]["Id"] = "10.0.0.11"
    elif fault == "extra_target":
        target_data["TargetHealthDescriptions"].append(copy.deepcopy(target))
    elif fault == "wrong_port":
        target["Target"]["Port"] = 9090
    result = discover(tmp_path, "aws", payload)
    assert result.returncode != 0, result.stdout
    assert result.stdout == ""
