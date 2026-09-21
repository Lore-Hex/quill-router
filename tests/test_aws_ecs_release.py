from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENV_SENTINEL = "fixture-env-value-must-not-appear-in-errors"


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


def task_definition(region: str = "eu-west-3") -> dict:
    payload = {"taskDefinition": {
        "taskDefinitionArn": "old:19", "revision": 19, "status": "ACTIVE",
        "family": {"eu-west-1": "tr-cp-euw1", "eu-west-3": "tr-cp-euw3"}[region],
        "networkMode": "awsvpc", "cpu": "256", "memory": "512",
        "taskRoleArn": "native-task-role", "executionRoleArn": "native-execution-role",
        "containerDefinitions": [{
            "name": "trusted-router", "image": "old@sha256:" + "a" * 64,
            "environment": [
                {"name": "TR_RELEASE", "value": "old-release"},
                {"name": "TR_SERVICE_SURFACE", "value": "observer"},
                {"name": "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED", "value": "true" if region == "eu-west-3" else "false"},
                {"name": "TR_ATTESTATION_PCR0", "value": "unchanged-pin"},
                {"name": "TR_CONFIG_SENTINEL", "value": ENV_SENTINEL},
                {"name": "RELEASE_COMMIT", "value": "old-release"},
            ],
            "secrets": [{"name": "TR_POSTGRES_DSN", "valueFrom": "arn:aws:secretsmanager:native"}],
            "portMappings": [{"containerPort": 8080}],
        }],
    }, "tags": [{"key": "owner", "value": "trustedrouter"}]}
    if region == "eu-west-3":
        container = payload["taskDefinition"]["containerDefinitions"][0]
        container["environment"].extend([
            {"name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL", "value": "http://fixture-clickhouse:8123"},
            {"name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER", "value": "fixture-user"},
            {"name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE", "value": "fixture-database"},
        ])
        container["secrets"].append({
            "name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD",
            "valueFrom": "arn:aws:secretsmanager:eu-west-3:330422590279:secret:fixture-clickhouse",
        })
    return payload


def expected_registration(original: dict, image: str) -> dict:
    expected = copy.deepcopy(original["taskDefinition"])
    for field in ("taskDefinitionArn", "revision", "status"):
        expected.pop(field)
    expected["tags"] = original["tags"]
    expected["containerDefinitions"][0]["image"] = image
    for entry in expected["containerDefinitions"][0]["environment"]:
        if entry["name"] in {"TR_RELEASE", "RELEASE_COMMIT"}:
            entry["value"] = "c" * 40
    return expected


@pytest.mark.parametrize(("region", "required_outbox"), [("eu-west-1", "false"), ("eu-west-3", "true")])
def test_image_release_update_preserves_all_native_runtime_configuration(region: str, required_outbox: str) -> None:
    original = task_definition(region)
    before = copy.deepcopy(original)
    image = "registry/trusted-router@sha256:" + "b" * 64
    result = load_builder()(original, image, "c" * 40, required_outbox)
    expected = expected_registration(original, image)
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


@pytest.mark.parametrize(("region", "required_outbox"), [("eu-west-1", "true"), ("eu-west-3", "false")])
def test_task_builder_refuses_outbox_drift(region: str, required_outbox: str) -> None:
    # Keep the live secret/flag pair consistent to isolate the regional expectation.
    payload = task_definition(region)
    before = copy.deepcopy(payload)
    with pytest.raises(ValueError, match="TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED"):
        load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, required_outbox)
    assert payload == before


@pytest.mark.parametrize(("region", "required_outbox"), [("eu-west-1", "true"), ("eu-west-3", "false")])
def test_task_builder_refuses_inconsistent_clickhouse_secret(region: str, required_outbox: str) -> None:
    payload = task_definition(region)
    payload["taskDefinition"]["containerDefinitions"][0]["environment"][2]["value"] = required_outbox
    with pytest.raises(ValueError, match="TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD"):
        load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, required_outbox)


@pytest.mark.parametrize("required_outbox", ["yes", "", "TRUE"])
def test_task_builder_refuses_invalid_required_outbox(required_outbox: str) -> None:
    payload = task_definition("eu-west-1")
    # Match the invalid live value so the equality check cannot mask validation.
    payload["taskDefinition"]["containerDefinitions"][0]["environment"][2]["value"] = required_outbox
    with pytest.raises(ValueError, match="invalid required TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED"):
        load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, required_outbox)


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
        print(json.dumps(json.loads(pathlib.Path(os.environ["ECS_DEFINITION"]).read_text())[region]))
    elif op == "ecs register-task-definition":
        data = json.loads(pathlib.Path(args[args.index("--cli-input-json") + 1][7:]).read_text())
        assert data["containerDefinitions"][0]["image"].endswith("@" + digest)
        with open(os.environ["ECS_REGISTRATIONS"], "a") as f:
            f.write(json.dumps([region, data]) + "\n")
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
    definitions: dict[str, dict] | None = None,
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
    definition.write_text(json.dumps(definitions if definitions is not None else {
        region: task_definition(region) for region in ("eu-west-1", "eu-west-3")
    }))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TR_CLOUD_", "TR_DEPLOY_"))}
    result = subprocess.run(  # noqa: S603 - copied script, all cloud and image tools stubbed
        [shutil.which("bash") or "/bin/bash", str(scripts / "aws_ecs_control_plane.sh")],
        env={**env, "PATH": f"{bin_dir}:{env['PATH']}", "ECS_STATE": str(state),
             "ECS_CALLS": str(calls), "ECS_DEFINITION": str(definition),
             "ECS_REGISTRATIONS": str(tmp_path / "registrations"),
             "ECS_UNLOCK": str(unlock), "ECS_FAIL_REGION": failure,
             "HARNESS_VERIFIER_RC": str(verifier_rc), **(extra_env or {})},
        capture_output=True, text=True, check=False, timeout=timeout,
    )
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert ENV_SENTINEL not in result.stdout + result.stderr
    return result, recorded


def assert_registrations_preserve_regional_configuration(tmp_path: Path) -> None:
    definitions = json.loads((tmp_path / "definition").read_text())
    registrations = [json.loads(line) for line in (tmp_path / "registrations").read_text().splitlines()]
    assert [region for region, _ in registrations] == ["eu-west-1", "eu-west-3"]
    for region, registered in registrations:
        image = f"330422590279.dkr.ecr.{region}.amazonaws.com/trusted-router@sha256:" + "b" * 64
        assert registered == expected_registration(definitions[region], image)


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
    if not failure:
        assert_registrations_preserve_regional_configuration(tmp_path)


@pytest.mark.parametrize("region", ["eu-west-1", "eu-west-3"])
@pytest.mark.parametrize("consistent_clickhouse", [False, True], ids=["flag-only", "consistent-secret"])
def test_ecs_rollout_refuses_outbox_drift_without_rolling_back_healthy_region(
    tmp_path: Path, region: str, consistent_clickhouse: bool,
) -> None:
    definitions = {name: task_definition(name) for name in ("eu-west-1", "eu-west-3")}
    container = definitions[region]["taskDefinition"]["containerDefinitions"][0]
    other = task_definition("eu-west-3" if region == "eu-west-1" else "eu-west-1")
    other_container = other["taskDefinition"]["containerDefinitions"][0]
    container["environment"][2]["value"] = other_container["environment"][2]["value"]
    if consistent_clickhouse:
        container["environment"] = other_container["environment"]
        container["secrets"] = other_container["secrets"]
    result, recorded = run_ecs_fixture(tmp_path, definitions=definitions)
    assert result.returncode != 0
    assert "Cannot safely clone the ECS workload; no runtime configuration printed" in result.stderr
    # The refusal names the setting (never a value) so the CI log explains itself.
    assert "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED" in result.stderr
    assert (tmp_path / "unlock").read_text().strip() == "released"
    mutations = [c for c in recorded if c[:3] in (
        ["aws", "ecs", "register-task-definition"], ["aws", "ecs", "update-service"],
    )]
    expected = [] if region == "eu-west-1" else [
        ("register-task-definition", "eu-west-1"), ("update-service", "eu-west-1"),
    ]
    assert [(c[2], c[c.index("--region") + 1]) for c in mutations] == expected
    if region == "eu-west-3":
        assert mutations[-1][mutations[-1].index("--task-definition") + 1] == (
            "arn:aws:ecs:eu-west-1:330422590279:task-definition/tr-cp:20"
        )
    assert json.loads((tmp_path / "state").read_text()) == (
        {} if region == "eu-west-1" else {"eu-west-1": "c" * 40}
    )


@pytest.mark.parametrize(("full_table", "short_table"), [
    ("REGIONS=(eu-west-1 eu-west-3)", "REGIONS=(eu-west-1)"),
    ("SERVICES=(tr-cp-euw1 tr-cp-euw3)", "SERVICES=(tr-cp-euw1)"),
    ("EXPECTED_OUTBOX=(false true)", "EXPECTED_OUTBOX=(false)"),
])
def test_ecs_rollout_refuses_misaligned_tables_before_registration(
    tmp_path: Path, full_table: str, short_table: str,
) -> None:
    source_root = tmp_path / "source"
    scripts = source_root / "scripts/deploy"
    scripts.mkdir(parents=True)
    for name in ("aws_ecs_control_plane.sh", "prepare_ecs_release.py", "cloud_complete_gate.sh"):
        shutil.copy2(ROOT / "scripts/deploy" / name, scripts / name)
    script = scripts / "aws_ecs_control_plane.sh"
    script.write_text(script.read_text().replace(full_table, short_table))
    result, recorded = run_ecs_fixture(tmp_path / "run", source_root=source_root)
    assert result.returncode != 0
    assert "REGIONS, SERVICES, and EXPECTED_OUTBOX must have the same length" in result.stderr
    assert not any(c[:3] == ["aws", "ecs", "register-task-definition"] for c in recorded)
    assert not any(c[:3] == ["aws", "ecs", "update-service"] for c in recorded)


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
        + "; ".join(failed_checks) + " (observed " + json.dumps(config, sort_keys=True)
        + ", controller " + json.dumps(settings["deploymentController"], sort_keys=True) + ")"
    ) in result.stderr
    assert not any(c[:3] == ["aws", "ecs", "register-task-definition"] for c in recorded)
    assert not any(c[:3] == ["aws", "ecs", "update-service"] for c in recorded)


@pytest.mark.parametrize(("settings", "failed_checks"), [
    ({"deploymentConfiguration": None},
     ["minimumHealthyPercent", "maximumPercent", "deploymentCircuitBreaker.enable", "deploymentCircuitBreaker.rollback"]),
    ({"deploymentConfiguration": {"minimumHealthyPercent": 100, "maximumPercent": 200, "deploymentCircuitBreaker": None}},
     ["deploymentCircuitBreaker.enable", "deploymentCircuitBreaker.rollback"]),
    ({"deploymentConfiguration": {"minimumHealthyPercent": None, "maximumPercent": None,
                                  "deploymentCircuitBreaker": {"enable": True, "rollback": True}}},
     ["minimumHealthyPercent", "maximumPercent"]),
    ({"deploymentConfiguration": {"minimumHealthyPercent": "100", "maximumPercent": 200,
                                  "deploymentCircuitBreaker": {"enable": True, "rollback": True}}},
     ["minimumHealthyPercent"]),
    ({"deploymentController": None}, ["deploymentController.type"]),
], ids=["null-configuration", "null-breaker", "null-percents", "string-minimum", "null-controller"])
def test_ecs_rollout_refuses_null_and_malformed_protections(
    tmp_path: Path, settings: dict, failed_checks: list[str],
) -> None:
    # JSON null, not an absent key: the API contract allows it and it must never pass.
    result, recorded = run_ecs_fixture(
        tmp_path, extra_env={"ECS_DEPLOYMENT_SETTINGS": json.dumps(settings)},
    )
    assert result.returncode != 0
    assert (
        "refusing rollout without healthy-capacity and automatic rollback protections: "
        + "; ".join(failed_checks) + " (observed "
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
    assert_registrations_preserve_regional_configuration(tmp_path)
