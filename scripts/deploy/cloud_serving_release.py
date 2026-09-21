#!/usr/bin/env python3
"""Read-only release evidence for digest-pinned GCP and the live AWS ECS fleet."""

from __future__ import annotations

import json
import re
import subprocess
import sys

AWS_TARGETS = (("eu-west-1", "tr-cp-euw1"), ("eu-west-3", "tr-cp-euw3"))
SHA = re.compile(r"[0-9a-f]{7,40}", re.IGNORECASE)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


def require(condition: bool) -> None:
    if not condition:
        raise ValueError("incomplete or inconsistent serving evidence")


def container_release(container: dict, env_field: str) -> str:
    image = container["image"]
    releases = [e.get("value", "") for e in container.get(env_field, [])
                if e.get("name") == "TR_RELEASE"]
    require(len(releases) <= 1)
    release = releases[0] if releases else ""
    if "@" in image:
        require(DIGEST.fullmatch(image.split("@", 1)[1]) is not None)
        require(SHA.fullmatch(release) is not None)
        return release
    tag = image.rsplit(":", 1)[-1]
    require(SHA.fullmatch(tag) is not None)
    if release:
        require(SHA.fullmatch(release) is not None)
        require(tag.lower().startswith(release.lower()) or release.lower().startswith(tag.lower()))
    return tag


def gcp_release(revision: dict) -> str:
    containers = revision["spec"]["containers"]
    require(len(containers) == 1)
    return container_release(containers[0], "env")


def aws_json(region: str, *args: str, query: str | None = None) -> dict:
    command = ["aws", *args, "--region", region, "--output", "json", "--no-cli-pager"]
    if query:
        command.extend(["--query", query])
    result = subprocess.run(  # noqa: S603 - fixed AWS read commands; never prints responses
        command, capture_output=True, check=True, text=True, timeout=30,
    )
    payload = json.loads(result.stdout)
    require(isinstance(payload, dict))
    return payload


def ecs_service(region: str, service: str) -> dict:
    payload = aws_json(region, "ecs", "describe-services", "--cluster", "tr-cp",
                       "--services", service)
    require(not payload.get("failures") and len(payload["services"]) == 1)
    result = payload["services"][0]
    desired = result["desiredCount"]
    require(result["status"] == "ACTIVE" and 0 < desired <= 100)
    require(result["runningCount"] == desired and result["pendingCount"] == 0)
    deployments = result["deployments"]
    require(len(deployments) == 1)
    require(deployments[0]["status"] == "PRIMARY")
    require(deployments[0]["rolloutState"] == "COMPLETED")
    require(deployments[0]["taskDefinition"] == result["taskDefinition"])
    return {k: result[k] for k in ("taskDefinition", "desiredCount", "loadBalancers")}


def ecs_region_release(region: str, service: str) -> str:
    before = ecs_service(region, service)
    definition = before["taskDefinition"]
    # Only release metadata is requested: no secret environment values enter
    # this process or error logs.
    payload = aws_json(
        region, "ecs", "describe-task-definition", "--task-definition", definition,
        query="{taskDefinition:{containerDefinitions:taskDefinition.containerDefinitions[]."
              "{name:name,image:image,environment:environment[?name==`TR_RELEASE`]}}}",
    )
    containers = payload["taskDefinition"]["containerDefinitions"]
    containers = [c for c in containers if c["name"] == "trusted-router"]
    require(len(containers) == 1)
    container = containers[0]
    release = container_release(container, "environment")
    image = container["image"]
    # Live ECS images are immutable. A mutable task-definition tag alone is
    # not proof of the image that already-running tasks pulled.
    require("@" in image)
    digest = image.split("@", 1)[1]
    arns = aws_json(region, "ecs", "list-tasks", "--cluster", "tr-cp",
                    "--service-name", service, "--desired-status", "RUNNING")["taskArns"]
    require(len(set(arns)) == len(arns) == before["desiredCount"])
    payload = aws_json(
        region, "ecs", "describe-tasks", "--cluster", "tr-cp", "--tasks", *arns,
        query="{failures:failures,tasks:tasks[].{taskArn:taskArn,"
              "taskDefinitionArn:taskDefinitionArn,lastStatus:lastStatus,"
              "containers:containers[].{name:name,lastStatus:lastStatus,image:image,"
              "imageDigest:imageDigest,networkInterfaces:networkInterfaces}}}",
    )
    tasks = payload["tasks"]
    require(not payload.get("failures") and len(tasks) == len(arns))
    require({t["taskArn"] for t in tasks} == set(arns))
    addresses = set()
    for task in tasks:
        require(task["taskDefinitionArn"] == definition and task["lastStatus"] == "RUNNING")
        running = [c for c in task["containers"] if c["name"] == "trusted-router"]
        require(len(running) == 1)
        current = running[0]
        require(current["lastStatus"] == "RUNNING")
        require(current["image"] == image and current["imageDigest"] == digest)
        interfaces = current["networkInterfaces"]
        require(len(interfaces) == 1)
        addresses.add(interfaces[0]["privateIpv4Address"])
    require(len(addresses) == len(arns))
    balancers = before["loadBalancers"]
    require(len(balancers) == 1 and balancers[0]["containerName"] == "trusted-router")
    balancer = balancers[0]
    targets = aws_json(region, "elbv2", "describe-target-health", "--target-group-arn",
                       balancer["targetGroupArn"])["TargetHealthDescriptions"]
    # A deregistered target stays listed as "draining" for the whole
    # deregistration delay (300 s here) after a rollout completes. It takes no
    # new requests, so it is not serving evidence; counting it made the
    # post-rollout check fail and roll a healthy release back. Everything that
    # is NOT draining must still match the running tasks exactly.
    targets = [t for t in targets if t["TargetHealth"]["State"] != "draining"]
    require(len(targets) == len(addresses))
    require({t["Target"]["Id"] for t in targets} == addresses)
    require(all(t["TargetHealth"]["State"] == "healthy"
                and t["Target"]["Port"] == balancer["containerPort"] for t in targets))
    # Re-read after the task/target checks to reject a rollout that started
    # while evidence was being collected.
    require(ecs_service(region, service) == before)
    return release


def aws_release() -> str:
    releases = [ecs_region_release(region, service) for region, service in AWS_TARGETS]
    require(len(set(releases)) == 1)
    return releases[0]


def main() -> int:
    try:
        if sys.argv[1:] == ["gcp"]:
            release = gcp_release(json.load(sys.stdin))
        elif sys.argv[1:] == ["aws"]:
            release = aws_release()
        elif (len(sys.argv) == 4 and sys.argv[1] == "aws-region"
              and tuple(sys.argv[2:]) in AWS_TARGETS):
            release = ecs_region_release(sys.argv[2], sys.argv[3])
        else:
            return 2
    except (KeyError, TypeError, ValueError, OSError, subprocess.SubprocessError):
        # Cloud responses may contain credentials or sensitive error details.
        # The caller reports UNKNOWN; never echo raw stdout/stderr here.
        return 1
    print(release)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
