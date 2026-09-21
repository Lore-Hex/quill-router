from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_aws_workflow_deploys_ecs_not_retired_apprunner() -> None:
    workflow = (ROOT / ".github/workflows/deploy-aws-control-plane.yml").read_text()
    assert "run: bash scripts/deploy/aws_ecs_control_plane.sh" in workflow
    assert "run: bash scripts/deploy/aws_eu_control_plane.sh" not in workflow


def load_builder():
    path = ROOT / "scripts/deploy/prepare_ecs_release.py"
    spec = importlib.util.spec_from_file_location("prepare_ecs_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.prepare


def task_definition() -> dict:
    return {"taskDefinition": {
        "taskDefinitionArn": "old:19", "revision": 19, "status": "ACTIVE",
        "family": "tr-cp-euw3", "networkMode": "awsvpc", "cpu": "256", "memory": "512",
        "taskRoleArn": "native-task-role", "executionRoleArn": "native-execution-role",
        "containerDefinitions": [{
            "name": "trusted-router", "image": "old@sha256:" + "a" * 64,
            "environment": [
                {"name": "TR_RELEASE", "value": "old-release"},
                {"name": "TR_SERVICE_SURFACE", "value": "observer"},
                {"name": "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED", "value": "true"},
                {"name": "TR_ATTESTATION_PCR0", "value": "unchanged-pin"},
            ],
            "secrets": [{"name": "TR_POSTGRES_DSN", "valueFrom": "arn:aws:secretsmanager:native"}],
            "portMappings": [{"containerPort": 8080}],
        }],
    }, "tags": [{"key": "owner", "value": "trustedrouter"}]}


def test_image_release_update_preserves_all_native_runtime_configuration() -> None:
    import copy

    original = task_definition()
    before = copy.deepcopy(original)
    image = "registry/trusted-router@sha256:" + "b" * 64
    result = load_builder()(original, image, "c" * 40)
    expected = copy.deepcopy(original["taskDefinition"])
    for field in ("taskDefinitionArn", "revision", "status"):
        expected.pop(field)
    expected["tags"] = original["tags"]
    expected["containerDefinitions"][0]["image"] = image
    expected["containerDefinitions"][0]["environment"][0]["value"] = "c" * 40
    assert result == expected
    assert original == before


@pytest.mark.parametrize("fault", ["unknown_field", "wrong_container", "outbox_off", "wrong_surface", "tag_only", "bad_release"])
def test_task_builder_fails_closed(fault: str) -> None:
    payload = task_definition()
    container = payload["taskDefinition"]["containerDefinitions"][0]
    image, release = "registry@sha256:" + "b" * 64, "c" * 40
    if fault == "unknown_field":
        payload["taskDefinition"]["futureRuntimeSetting"] = "must-not-drop"
    elif fault == "wrong_container":
        container["name"] = "other-app"
    elif fault == "outbox_off":
        container["environment"][2]["value"] = "false"
    elif fault == "wrong_surface":
        container["environment"][1]["value"] = "combined"
    elif fault == "tag_only":
        image = "registry:latest"
    elif fault == "bad_release":
        release = "main"
    with pytest.raises(ValueError):
        load_builder()(payload, image, release)


CLI_STUB = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
with open(os.environ["ECS_CALLS"], "a") as f:
    f.write(json.dumps([tool, *args]) + "\n")
sha, digest = "c" * 40, "sha256:" + "b" * 64
state_path = pathlib.Path(os.environ["ECS_STATE"])
state = json.loads(state_path.read_text())
region = args[args.index("--region") + 1] if "--region" in args else ""
if tool == "git":
    if args[0] == "rev-parse": print(sha)
elif tool == "gh": print("success")
elif tool == "gcloud":
    print(digest if args[0] == "artifacts" else "fixture-token")
elif tool == "docker":
    if args[0] == "login": sys.stdin.read()
elif tool == "aws":
    op = " ".join(args[:2])
    if op == "sts get-caller-identity": print("330422590279")
    elif op == "ecr get-login-password": print("fixture-password")
    elif op == "ecr describe-images": print(digest)
    elif op == "ecs describe-services":
        service = {"taskDefinition": "old:19",
            "deploymentController": {"type": "ECS"},
            "deploymentConfiguration": {"bakeTimeInMinutes": 0,
                "deploymentCircuitBreaker": {"enable": True, "resetOnHealthyTask": True,
                    "rollback": True, "thresholdConfiguration": {"type": "BOUNDED_PERCENT", "value": 50}},
                "maximumPercent": 200, "minimumHealthyPercent": 100, "strategy": "ROLLING"}}
        service.update(json.loads(os.environ.get("ECS_DEPLOYMENT_SETTINGS", "{}")))
        print(json.dumps({"services": [service]}))
    elif op == "ecs describe-task-definition":
        print(pathlib.Path(os.environ["ECS_DEFINITION"]).read_text())
    elif op == "ecs register-task-definition":
        data = json.loads(pathlib.Path(args[args.index("--cli-input-json") + 1][7:]).read_text())
        assert data["containerDefinitions"][0]["image"].endswith("@" + digest)
        print("arn:aws:ecs:" + region + ":330422590279:task-definition/tr-cp:20")
    elif op == "ecs update-service":
        definition = args[args.index("--task-definition") + 1]
        state[region] = "old" if definition == "old:19" else sha
        state_path.write_text(json.dumps(state))
        print(definition)
    elif op == "ecs wait":
        if region == os.environ.get("ECS_FAIL_REGION") and state.get(region) == sha: sys.exit(1)
    else: sys.exit(2)
else: sys.exit(2)
'''


def run_ecs_fixture(
    tmp_path: Path, *, failure: str = "", verifier_rc: int = 0,
    source_root: Path = ROOT, extra_env: dict | None = None, timeout: int = 120,
) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    scripts = tmp_path / "scripts/deploy"
    scripts.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("aws_ecs_control_plane.sh", "prepare_ecs_release.py", "cloud_complete_gate.sh"):
        shutil.copy2(source_root / "scripts/deploy" / name, scripts / name)
    (scripts / "deploy_mutex.sh").write_text(
        'deploy_mutex_acquire() { DEPLOY_MUTEX_SCOPE_OWNS_LOCK=1; }; '
        'deploy_mutex_release() { echo released > "$ECS_UNLOCK"; }\n'
    )
    (scripts / "cloud_bake_gate.sh").write_text(
        'cloud_bake_gate() { [ "${ECS_FAIL_REGION:-}" != gate ]; }\n'
    )
    # Keep the real shared gate library, so its status and diagnostic wording
    # are exercised by the common completeness harness too.
    (scripts / "verify_cloud_complete.sh").write_text(
        'printf \'["verify_cloud_complete.sh","%s"]\\n\' "$1" >> "$ECS_CALLS"\n'
        'exit "$HARNESS_VERIFIER_RC"\n'
    )
    (scripts / "cloud_serving_release.py").write_text(
        'import json, os, sys\n'
        'state=json.load(open(os.environ["ECS_STATE"]))\n'
        'values=list(state.values())\n'
        'if sys.argv[1] == "aws-region": print(state.get(sys.argv[2], "old"))\n'
        'else:\n'
        '    assert not values or len(set(values)) == 1\n'
        '    print(values[0] if values else "old")\n'
    )
    for tool in ("git", "gh", "aws", "gcloud", "docker"):
        executable = bin_dir / tool
        executable.write_text(CLI_STUB)
        executable.chmod(0o755)
    state, calls, definition, unlock = (tmp_path / name for name in ("state", "calls", "definition", "unlock"))
    state.write_text("{}")
    definition.write_text(json.dumps(task_definition()))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TR_CLOUD_", "TR_DEPLOY_"))}
    result = subprocess.run(  # noqa: S603 - copied script, all cloud and image tools stubbed
        [shutil.which("bash") or "/bin/bash", str(scripts / "aws_ecs_control_plane.sh")],
        env={**env, "PATH": f"{bin_dir}:{env['PATH']}", "ECS_STATE": str(state),
             "ECS_CALLS": str(calls), "ECS_DEFINITION": str(definition),
             "ECS_UNLOCK": str(unlock), "ECS_FAIL_REGION": failure,
             "HARNESS_VERIFIER_RC": str(verifier_rc), **(extra_env or {})},
        capture_output=True, text=True, check=False, timeout=timeout,
    )
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    return result, recorded


@pytest.mark.parametrize("failure", ["", "gate", "eu-west-1", "eu-west-3"])
def test_ecs_rollout_is_gated_sequential_and_rolls_back_failed_region(tmp_path: Path, failure: str) -> None:
    result, recorded = run_ecs_fixture(tmp_path, failure=failure)
    assert (result.returncode == 0) == (failure == ""), result.stderr
    assert (tmp_path / "unlock").read_text().strip() == "released"
    updates = [c for c in recorded if c[:3] == ["aws", "ecs", "update-service"]]
    regions = [c[c.index("--region") + 1] for c in updates]
    expected = {"": ["eu-west-1", "eu-west-3"], "gate": [],
                "eu-west-1": ["eu-west-1", "eu-west-1"],
                "eu-west-3": ["eu-west-1", "eu-west-3", "eu-west-3"]}
    assert regions == expected[failure]
    if failure == "gate":
        assert not any(c[0] == "docker" for c in recorded)
    elif failure:
        assert updates[-1][updates[-1].index("--task-definition") + 1] == "old:19"
    assert not any("apprunner" in c for c in recorded)


@pytest.mark.parametrize(("field", "value", "failed_checks"), [
    ("deploymentCircuitBreaker.enable", False, ["deploymentCircuitBreaker.enable"]),
    ("deploymentCircuitBreaker.rollback", False, ["deploymentCircuitBreaker.rollback"]),
    ("deploymentCircuitBreaker", None,
     ["deploymentCircuitBreaker.enable", "deploymentCircuitBreaker.rollback"]),
    ("deploymentCircuitBreaker.enable", "true", ["deploymentCircuitBreaker.enable"]),
    ("deploymentCircuitBreaker.enable", 1, ["deploymentCircuitBreaker.enable"]),
    ("deploymentCircuitBreaker.rollback", "true", ["deploymentCircuitBreaker.rollback"]),
    ("deploymentCircuitBreaker.rollback", 1, ["deploymentCircuitBreaker.rollback"]),
    ("deploymentCircuitBreaker.enable", None, ["deploymentCircuitBreaker.enable"]),
    ("deploymentCircuitBreaker.rollback", None, ["deploymentCircuitBreaker.rollback"]),
    ("minimumHealthyPercent", 50, ["minimumHealthyPercent"]),
    ("maximumPercent", 150, ["maximumPercent"]),
    ("deploymentController.type", "CODE_DEPLOY", ["deploymentController.type"]),
    ("strategy", "BLUE_GREEN", ["strategy"]),
])
def test_ecs_rollout_refuses_missing_protections_before_registration(
    tmp_path: Path, field: str, value: object, failed_checks: list[str],
) -> None:
    config = {"minimumHealthyPercent": 100, "maximumPercent": 200, "strategy": "ROLLING",
              "deploymentCircuitBreaker": {"enable": True, "rollback": True}}
    settings = {"deploymentController": {"type": "ECS"}, "deploymentConfiguration": config}
    target = settings if field.startswith("deploymentController.") else config
    *parents, key = field.split(".")
    for parent in parents:
        target = target[parent]
    if value is None:
        target.pop(key)
    else:
        target[key] = value
    result, recorded = run_ecs_fixture(
        tmp_path, extra_env={"ECS_DEPLOYMENT_SETTINGS": json.dumps(settings)},
    )
    assert result.returncode != 0
    assert (
        "refusing rollout without healthy-capacity and automatic rollback protections: "
        + "; ".join(failed_checks) + " (observed " + json.dumps(config, sort_keys=True) + ")"
    ) in result.stderr
    assert not any(c[:3] == ["aws", "ecs", "register-task-definition"] for c in recorded)
    assert not any(c[:3] == ["aws", "ecs", "update-service"] for c in recorded)


@pytest.mark.parametrize("future_field", [False, True], ids=["legacy", "future-sibling"])
def test_ecs_rollout_accepts_legacy_and_future_breaker_fields(tmp_path: Path, future_field: bool) -> None:
    config = {"minimumHealthyPercent": 100, "maximumPercent": 200,
              "deploymentCircuitBreaker": {"enable": True, "rollback": True}}
    if future_field:
        config["deploymentCircuitBreaker"]["someFutureField"] = {"x": 1}
    result, recorded = run_ecs_fixture(
        tmp_path, extra_env={"ECS_DEPLOYMENT_SETTINGS": json.dumps({"deploymentConfiguration": config})},
    )
    assert result.returncode == 0, result.stderr
    updates = [c for c in recorded if c[:3] == ["aws", "ecs", "update-service"]]
    assert [c[c.index("--region") + 1] for c in updates] == ["eu-west-1", "eu-west-3"]
