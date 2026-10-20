from __future__ import annotations

import copy
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.deploy_script_harness import harness_timeout_scale

ROOT = Path(__file__).resolve().parents[1]
ENV_SENTINEL = "fixture-env-value-must-not-appear-in-errors"
READER_ARN = "arn:aws:secretsmanager:eu-west-3:330422590279:secret:quill/tr-eu-clickhouse-read-password-fixture"


def test_aws_workflow_deploys_ecs_not_retired_apprunner() -> None:
    workflow = (ROOT / ".github/workflows/deploy-aws-control-plane.yml").read_text()
    assert "run: bash ../ops/scripts/deploy/aws_ecs_control_plane.sh" in workflow
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
                {"name": "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED", "value": "true"},
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
            {"name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER", "value": "default"},
            {"name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE", "value": "default"},
        ])
        container["secrets"].append({
            "name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD",
            "valueFrom": "arn:aws:secretsmanager:eu-west-3:330422590279:secret:fixture-clickhouse",
        })
    return payload


def expected_registration(original: dict, image: str, backend: str = "postgres") -> dict:
    expected = copy.deepcopy(original["taskDefinition"])
    for field in ("taskDefinitionArn", "revision", "status"):
        expected.pop(field)
    expected["tags"] = original["tags"]
    expected["containerDefinitions"][0]["image"] = image
    for entry in expected["containerDefinitions"][0]["environment"]:
        if entry["name"] in {"TR_RELEASE", "RELEASE_COMMIT"}:
            entry["value"] = "c" * 40
        elif entry["name"] == "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED":
            entry["value"] = "true"
    expected["containerDefinitions"][0]["environment"].append(
        {"name": "TR_REMEDIATOR_IN_PROCESS_ENABLED", "value": "false"}
    )
    container = expected["containerDefinitions"][0]
    container["environment"].append({"name": "TR_RECORD_READ_BACKEND", "value": backend})
    if backend == "clickhouse":
        for entry in container["environment"]:
            if entry["name"] == "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER":
                entry["value"] = "tr_control_read"
        for secret in container["secrets"]:
            if secret["name"] == "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD":
                secret["valueFrom"] = READER_ARN
    return expected


@pytest.mark.parametrize("tags", [None, []], ids=["absent", "empty"])
def test_untagged_task_definition_registers_without_a_tags_field(tags: list | None) -> None:
    # describe-task-definition returns no tags for an untagged definition, and
    # RegisterTaskDefinition rejects an empty list ("Tags can not be empty").
    payload = task_definition("eu-west-3")
    if tags is None:
        payload.pop("tags")
    else:
        payload["tags"] = tags
    result = load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, "true")
    assert "tags" not in result


def test_tagged_task_definition_keeps_its_tags() -> None:
    payload = task_definition("eu-west-3")
    result = load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, "true")
    assert result["tags"] == payload["tags"] and result["tags"]


@pytest.mark.parametrize("region", ["eu-west-1", "eu-west-3"])
@pytest.mark.parametrize("live_outbox", ["false", "true"])
def test_image_release_update_enables_outbox_and_preserves_native_configuration(region: str, live_outbox: str) -> None:
    original = task_definition(region)
    original["taskDefinition"]["containerDefinitions"][0]["environment"][2]["value"] = live_outbox
    before = copy.deepcopy(original)
    image = "registry/trusted-router@sha256:" + "b" * 64
    backend = "clickhouse" if region == "eu-west-3" else "postgres"
    result = load_builder()(original, image, "c" * 40, "true", backend, READER_ARN)
    expected = expected_registration(original, image, backend)
    assert result == expected
    assert original == before


@pytest.mark.parametrize("fault", ["unknown_field", "wrong_container", "outbox_invalid", "wrong_surface", "tag_only", "bad_release"])
def test_task_builder_fails_closed(fault: str) -> None:
    payload = task_definition()
    container = payload["taskDefinition"]["containerDefinitions"][0]
    image, release = "registry@sha256:" + "b" * 64, "c" * 40
    if fault == "unknown_field":
        payload["taskDefinition"]["futureRuntimeSetting"] = "must-not-drop"
    elif fault == "wrong_container":
        container["name"] = "other-app"
    elif fault == "outbox_invalid":
        container["environment"][2]["value"] = ""
    elif fault == "wrong_surface":
        container["environment"][1]["value"] = "combined"
    elif fault == "tag_only":
        image = "registry:latest"
    elif fault == "bad_release":
        release = "main"
    with pytest.raises(ValueError):
        load_builder()(payload, image, release)


def _in_process_values(result: dict) -> list[str]:
    env = result["containerDefinitions"][0]["environment"]
    return [e["value"] for e in env if e["name"] == "TR_REMEDIATOR_IN_PROCESS_ENABLED"]


def test_release_adds_the_in_process_remediator_pin_when_absent() -> None:
    # The live AWS observer definitions carry no TR_REMEDIATOR_IN_PROCESS_ENABLED.
    payload = task_definition("eu-west-3")
    assert _in_process_values(payload["taskDefinition"]) == []
    result = load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, "true")
    assert _in_process_values(result) == ["false"]


def test_release_keeps_an_existing_false_pin_without_duplicating_it() -> None:
    payload = task_definition("eu-west-1")
    payload["taskDefinition"]["containerDefinitions"][0]["environment"].append(
        {"name": "TR_REMEDIATOR_IN_PROCESS_ENABLED", "value": "false"}
    )
    result = load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, "true")
    assert _in_process_values(result) == ["false"]


@pytest.mark.parametrize("value", ["true", "True", "1", ""])
def test_release_refuses_an_in_process_remediator_on_aws(value: str) -> None:
    payload = task_definition("eu-west-3")
    payload["taskDefinition"]["containerDefinitions"][0]["environment"].append(
        {"name": "TR_REMEDIATOR_IN_PROCESS_ENABLED", "value": value}
    )
    before = copy.deepcopy(payload)
    with pytest.raises(ValueError, match="TR_REMEDIATOR_IN_PROCESS_ENABLED must be false"):
        load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, "true")
    assert payload == before


@pytest.mark.parametrize("region", ["eu-west-1", "eu-west-3"])
def test_task_builder_refuses_to_disable_an_enabled_outbox(region: str) -> None:
    payload = task_definition(region)
    before = copy.deepcopy(payload)
    with pytest.raises(ValueError, match="TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED"):
        load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, "false")
    assert payload == before


def test_task_builder_enables_ireland_without_clickhouse_credentials() -> None:
    payload = task_definition("eu-west-1")
    payload["taskDefinition"]["containerDefinitions"][0]["environment"][2]["value"] = "false"
    result = load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, "true")
    container = result["containerDefinitions"][0]
    values = {e["name"]: e["value"] for e in container["environment"]}
    assert values["TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED"] == "true"
    assert "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL" not in values
    assert "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD" not in values
    assert all(s["name"] != "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_PASSWORD" for s in container["secrets"])


@pytest.mark.parametrize("required_outbox", ["yes", "", "TRUE"])
def test_task_builder_refuses_invalid_required_outbox(required_outbox: str) -> None:
    payload = task_definition("eu-west-1")
    # Match the invalid live value so the equality check cannot mask validation.
    payload["taskDefinition"]["containerDefinitions"][0]["environment"][2]["value"] = required_outbox
    with pytest.raises(ValueError, match="invalid required TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED"):
        load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, required_outbox)


@pytest.mark.parametrize("backend", ["postgres", "clickhouse"])
@pytest.mark.parametrize("live_backend", [None, "postgres", "clickhouse"])
@pytest.mark.parametrize("allow_rollback", [False, True])
def test_record_read_transitions(backend: str, live_backend: str | None, allow_rollback: bool) -> None:
    payload = task_definition()
    if live_backend is not None:
        payload["taskDefinition"]["containerDefinitions"][0]["environment"].append(
            {"name": "TR_RECORD_READ_BACKEND", "value": live_backend}
        )
    before = copy.deepcopy(payload)
    image = "registry@sha256:" + "b" * 64
    args = (payload, image, "c" * 40, "true", backend, READER_ARN, allow_rollback)
    if (live_backend, backend, allow_rollback) == ("clickhouse", "postgres", False):
        with pytest.raises(ValueError, match="TR_ECS_ALLOW_RECORD_READ_ROLLBACK=true"):
            load_builder()(*args)
    else:
        result = load_builder()(*args)
        container = result["containerDefinitions"][0]
        values = {e["name"]: e["value"] for e in container["environment"]}
        assert values["TR_RECORD_READ_BACKEND"] == backend
        assert sum(e["name"] == "TR_RECORD_READ_BACKEND" for e in container["environment"]) == 1
        if backend == "clickhouse":
            assert values["TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER"] == "tr_control_read"
            assert container["secrets"][-1]["valueFrom"] == READER_ARN
        else:
            assert values["TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER"] == "default"
            assert container["secrets"] == before["taskDefinition"]["containerDefinitions"][0]["secrets"]
        # A subsequent clone with the same release and credentials is identical.
        next_payload = {
            "taskDefinition": {k: v for k, v in result.items() if k != "tags"},
            "tags": result["tags"],
        }
        assert load_builder()(next_payload, *args[1:]) == result
    assert payload == before


@pytest.mark.parametrize("value", ["", "invalid", "CLICKHOUSE"])
@pytest.mark.parametrize("allow_rollback", [False, True])
def test_record_reads_refuse_unknown_live_values(value: str, allow_rollback: bool) -> None:
    payload = task_definition()
    payload["taskDefinition"]["containerDefinitions"][0]["environment"].append(
        {"name": "TR_RECORD_READ_BACKEND", "value": value}
    )
    with pytest.raises(ValueError, match="live TR_RECORD_READ_BACKEND differs"):
        load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, "true", "clickhouse",
                       READER_ARN, allow_rollback)


@pytest.mark.parametrize("value", ["", "invalid", "CLICKHOUSE"])
def test_record_reads_refuse_invalid_expectation(value: str) -> None:
    with pytest.raises(ValueError, match="invalid required TR_RECORD_READ_BACKEND"):
        load_builder()(task_definition(), "registry@sha256:" + "b" * 64, "c" * 40, "true", value)


@pytest.mark.parametrize("url", [None, "", "  "])
def test_clickhouse_record_reads_require_cloned_url(url: str | None) -> None:
    payload = task_definition("eu-west-1")
    if url is not None:
        payload["taskDefinition"]["containerDefinitions"][0]["environment"].append(
            {"name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL", "value": url}
        )
    with pytest.raises(ValueError, match="cloned TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL"):
        load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, "true", "clickhouse", READER_ARN)


@pytest.mark.parametrize("arn", ["", "None", "quill/tr-eu-clickhouse-read-password"])
def test_clickhouse_record_reads_require_reader_arn(arn: str) -> None:
    with pytest.raises(ValueError, match="reader secret ARN"):
        load_builder()(task_definition(), "registry@sha256:" + "b" * 64, "c" * 40, "true", "clickhouse", arn)


def test_record_read_grant_is_scoped_to_reader_secret_metadata() -> None:
    policy = (ROOT / "infra/aws_ecs_control_reader.tf").read_text()
    assert 'data "aws_secretsmanager_secret" "tr_eu_clickhouse_control_reader"' in policy
    assert 'name = "quill/tr-eu-clickhouse-read-password"' in policy
    assert 'role = "tr-cp-exec"' in policy
    assert 'Action   = "secretsmanager:GetSecretValue"' in policy
    assert 'Resource = data.aws_secretsmanager_secret.tr_eu_clickhouse_control_reader.arn' in policy
    assert "aws_secretsmanager_secret_version" not in policy
    assert "*" not in policy


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
elif tool == "sleep": pass
elif tool == "curl":
    header = sys.stdin.read()
    expected = "Authorization: bearer " + os.environ.get("ECS_REQUEST_TOKEN", "") + "\n"
    if header != expected or os.environ.get("ECS_FAIL_OIDC"): sys.exit(22)
    print(json.dumps({"value": os.environ["ECS_OIDC_TOKEN"]}))
    # A short transfer: complete JSON on stdout, then curl's exit 18.
    if os.environ.get("ECS_PARTIAL_OIDC"): sys.exit(18)
elif tool == "aws":
    op = " ".join(args[:2])
    if os.environ.get("ECS_AWS_SESSIONS"):
        with open(os.environ["ECS_AWS_SESSIONS"], "a") as f:
            f.write(json.dumps([op, os.environ.get("AWS_SESSION_TOKEN", "")]) + "\n")
    if op == "sts get-caller-identity":
        print("330422590279")
        # The account printed, then a failure: the status must still count.
        if os.environ.get("ECS_CALLER_FAILS_AFTER_RENEWAL") and os.environ.get("AWS_SESSION_TOKEN") == "renewed-session":
            sys.exit(254)
    elif op == "sts assume-role-with-web-identity":
        # The ID token arrives on stdin: never in argv, never in a file.
        assert args[args.index("--web-identity-token") + 1] == "file:///dev/stdin"
        assert sys.stdin.read() == os.environ["ECS_OIDC_TOKEN"]
        if os.environ.get("ECS_FAIL_ASSUME"): sys.exit(254)
        if os.environ.get("ECS_MALFORMED_STS"):
            # What the AWS CLI does with a response it cannot parse: it prints
            # the raw response, credentials included, to stderr.
            sys.stderr.write("Unable to parse response: <AccessKeyId>ASIARENEWED</AccessKeyId>"
                             "<SecretAccessKey>renewed-secret</SecretAccessKey>"
                             "<SessionToken>renewed-session</SessionToken>\n")
            sys.exit(255)
        print("ASIARENEWED\trenewed-secret\trenewed-session")
    elif op == "ecr get-login-password": print("fixture-password")
    elif op == "ecr describe-images": print(digest)
    elif op == "secretsmanager describe-secret":
        assert args[args.index("--secret-id") + 1] == "quill/tr-eu-clickhouse-read-password"
        assert args[args.index("--query") + 1] == "ARN"
        if os.environ.get("ECS_READER_LOOKUP_FAILS"): sys.exit(254)
        print(os.environ.get("ECS_READER_ARN", "arn:aws:secretsmanager:" + region
              + ":330422590279:secret:quill/tr-eu-clickhouse-read-password-fixture"))
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
        if "tags" in data and not data["tags"]:
            # The real API: ClientException "Tags can not be empty."
            sys.stderr.write("Tags can not be empty.\n"); sys.exit(254)
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


READER_STUB = r'''import json, os, pathlib, sys
state = json.loads(pathlib.Path(os.environ["ECS_STATE"]).read_text())
key = ":".join(sys.argv[1:3]) if sys.argv[1] == "aws-region" else "aws"
if sys.argv[1] == "aws-region":
    value = state.get(sys.argv[2], "old")
    phase = "preflight" if sys.argv[2] not in state else "rollback" if value == "old" else "rollout"
else:
    values = list(state.values())
    assert not values or len(set(values)) == 1
    value = values[0] if values else "old"
    phase = "rollout" if values else "preflight"
with open(os.environ["ECS_CALLS"], "a") as f:
    f.write(json.dumps(["reader", key, phase]) + "\n")
if phase != "preflight":
    counter_path = pathlib.Path(os.environ["ECS_READER_COUNTS"])
    counts = json.loads(counter_path.read_text())
    counter = phase + ":" + key
    counts[counter] = counts.get(counter, 0) + 1
    counter_path.write_text(json.dumps(counts))
    prefix = "ECS_ROLLBACK_READER_" if phase == "rollback" else "ECS_READER_"
    for kind, output, status in (("UNREADABLE", "", 1), ("WRONG", "wrong-release", 0),
                                 ("ERROR_OUTPUT", value, 1)):
        limits = dict(item.split("=") for item in os.environ.get(prefix + kind, "").split(",") if item)
        if counts[counter] <= int(limits.get(key, "0")):
            if output: print(output)
            sys.exit(status)
print(value)
'''


#: What the ECS rollout needs from scripts/deploy, copied verbatim into a fixture.
ECS_SCRIPT_FILES = (
    "aws_ecs_control_plane.sh", "_aws_session.sh", "prepare_ecs_release.py", "cloud_complete_gate.sh",
)


def run_ecs_fixture(
    tmp_path: Path, *, failure: str = "", verifier_rc: int = 0,
    source_root: Path = ROOT, extra_env: dict | None = None, timeout: int = 120,
    definitions: dict[str, dict] | None = None,
) -> tuple[subprocess.CompletedProcess, list[list[str]]]:
    scripts = tmp_path / "scripts/deploy"
    scripts.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ECS_SCRIPT_FILES:
        shutil.copy2(source_root / "scripts/deploy" / name, scripts / name)
    (scripts / "deploy_mutex.sh").write_text(
        'deploy_mutex_acquire() { DEPLOY_MUTEX_SCOPE_OWNS_LOCK=1; '
        'wc -l < "$ECS_CALLS" | tr -d " " > "${ECS_UNLOCK}.acquired"; }; '
        'deploy_mutex_assert() { :; }; '
        'deploy_mutex_finish() { echo "$1" > "$ECS_UNLOCK"; }\n'
    )
    (scripts / "cloud_bake_gate.sh").write_text(
        'cloud_bake_gate() { [ "${ECS_SUPERSEDED:-}" != 1 ] || return 75; '
        '[ "${ECS_FAIL_REGION:-}" != gate ]; }\n'
    )
    # Keep the real shared gate library, so its status and diagnostic wording
    # are exercised by the common completeness harness too.
    (scripts / "verify_cloud_complete.sh").write_text(
        'printf \'["verify_cloud_complete.sh","%s"]\\n\' "$1" >> "$ECS_CALLS"\n'
        'exit "$HARNESS_VERIFIER_RC"\n'
    )
    (scripts / "cloud_serving_release.py").write_text(READER_STUB)
    for tool in ("git", "gh", "aws", "gcloud", "docker", "sleep", "curl"):
        executable = bin_dir / tool
        executable.write_text(CLI_STUB)
        executable.chmod(0o755)
    state, calls, definition, unlock = (tmp_path / name for name in ("state", "calls", "definition", "unlock"))
    state.write_text("{}")
    calls.write_text("")
    reader_counts = tmp_path / "reader-counts"
    reader_counts.write_text("{}")
    definition.write_text(json.dumps(definitions if definitions is not None else {
        region: task_definition(region) for region in ("eu-west-1", "eu-west-3")
    }))
    # A developer's or runner's own AWS credentials and OIDC request variables
    # never reach the copied script; tests that need them pass them in.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("TR_CLOUD_", "TR_DEPLOY_", "TR_ECS_", "TR_RECORD_READ_BACKEND_", "ECS_", "ACTIONS_ID_TOKEN_", "AWS_"))}
    result = subprocess.run(  # noqa: S603 - copied script, all cloud and image tools stubbed
        [shutil.which("bash") or "/bin/bash", str(scripts / "aws_ecs_control_plane.sh")],
        env={**env, "PATH": f"{bin_dir}:{env['PATH']}", "ECS_STATE": str(state),
             "ECS_CALLS": str(calls), "ECS_DEFINITION": str(definition),
             "ECS_READER_COUNTS": str(reader_counts),
             "ECS_REGISTRATIONS": str(tmp_path / "registrations"),
             "ECS_UNLOCK": str(unlock), "ECS_FAIL_REGION": failure,
             "HARNESS_VERIFIER_RC": str(verifier_rc),
             # The state once Paris moves to ClickHouse reads (its own
             # change, after the reader-secret grant applies); the shipped
             # default is pinned by test_paris_ships_on_postgres_until_the_grant.
             "TR_RECORD_READ_BACKEND_EU_WEST_3": "clickhouse", **(extra_env or {})},
        capture_output=True, text=True, check=False, timeout=timeout * harness_timeout_scale(),
    )
    recorded = [json.loads(line) for line in calls.read_text().splitlines()]
    assert ENV_SENTINEL not in result.stdout + result.stderr
    assert not any(c[:3] == ["aws", "secretsmanager", "get-secret-value"] for c in recorded)
    return result, recorded


def assert_registrations_preserve_regional_configuration(tmp_path: Path) -> None:
    definitions = json.loads((tmp_path / "definition").read_text())
    registrations = [json.loads(line) for line in (tmp_path / "registrations").read_text().splitlines()]
    assert [region for region, _ in registrations] == ["eu-west-1", "eu-west-3"]
    for region, registered in registrations:
        image = f"330422590279.dkr.ecr.{region}.amazonaws.com/trusted-router@sha256:" + "b" * 64
        backend = "clickhouse" if region == "eu-west-3" else "postgres"
        assert registered == expected_registration(definitions[region], image, backend)


def test_superseded_queued_promotion_is_clean_noop(tmp_path: Path) -> None:
    result, recorded = run_ecs_fixture(tmp_path, extra_env={"ECS_SUPERSEDED": "1"})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "unlock").read_text().strip() == "0"
    assert not any(c[0] == "docker" for c in recorded)
    assert all(c[:3] == ["aws", "sts", "get-caller-identity"] for c in recorded if c[0] == "aws")


@pytest.mark.parametrize("failure", ["", "gate", "eu-west-1", "eu-west-3"])
def test_ecs_rollout_is_gated_sequential_and_rolls_back_failed_region(tmp_path: Path, failure: str) -> None:
    result, recorded = run_ecs_fixture(tmp_path, failure=failure)
    assert (result.returncode == 0) == (failure == ""), result.stderr
    assert (tmp_path / "unlock").read_text().strip() == str(result.returncode)
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


@pytest.mark.parametrize(("kind", "key", "failures"), [
    ("UNREADABLE", "aws-region:eu-west-1", 3),
    ("UNREADABLE", "aws", 2),
    ("WRONG", "aws-region:eu-west-1", 3),
    ("ERROR_OUTPUT", "aws-region:eu-west-1", 3),
])
def test_ecs_verification_retries_until_exact_success(
    tmp_path: Path, kind: str, key: str, failures: int,
) -> None:
    result, recorded = run_ecs_fixture(tmp_path, extra_env={f"ECS_READER_{kind}": f"{key}={failures}"})
    assert result.returncode == 0, result.stderr
    updates = [c for c in recorded if c[:3] == ["aws", "ecs", "update-service"]]
    assert [c[c.index("--region") + 1] for c in updates] == ["eu-west-1", "eu-west-3"]
    assert all(c[c.index("--task-definition") + 1] != "old:19" for c in updates)
    assert recorded.count(["reader", key, "rollout"]) == failures + 1
    assert [c for c in recorded if c[0] == "sleep"] == [["sleep", "15"]] * failures
    if key == "aws-region:eu-west-1":
        assert recorded.index(updates[0]) < recorded.index(["reader", key, "rollout"])
        assert max(i for i, c in enumerate(recorded) if c == ["reader", key, "rollout"]) < recorded.index(updates[1])
    else:
        assert recorded.index(updates[-1]) < recorded.index(["reader", key, "rollout"])
    assert (tmp_path / "unlock").read_text().strip() == str(result.returncode)


@pytest.mark.parametrize(("region", "kind", "timeout_seconds", "attempts"), [
    ("eu-west-1", "UNREADABLE", "30", 2),
    ("eu-west-3", "WRONG", "30", 2),
    ("eu-west-1", "UNREADABLE", "31", 3),
    ("eu-west-1", "UNREADABLE", "1", 1),
    ("eu-west-1", "UNREADABLE", None, 32),
])
def test_ecs_verification_attempt_budget_rolls_back_only_active_region(
    tmp_path: Path, region: str, kind: str, timeout_seconds: str | None, attempts: int,
) -> None:
    key = f"aws-region:{region}"
    env = {f"ECS_READER_{kind}": f"{key}=10000"}
    if timeout_seconds is not None:
        env.update(TR_ECS_VERIFY_TIMEOUT_SECONDS=timeout_seconds, TR_ECS_VERIFY_INTERVAL_SECONDS="15")
    # The exact counts below are what prove the loop is bounded by attempts:
    # with the no-op sleep, a wall-clock loop reads until its real deadline,
    # far more than `attempts` times, or for the 480-second default runs into
    # the fixture's timeout. That timeout is a hang guard, not a speed budget:
    # a loaded parallel run took over 10 s for these 92 stubbed calls.
    result, recorded = run_ecs_fixture(tmp_path, extra_env=env)
    assert result.returncode != 0
    assert recorded.count(["reader", key, "rollout"]) == attempts
    assert [c for c in recorded if c[0] == "sleep"] == [["sleep", "15"]] * (attempts - 1)
    assert f"last observed={'UNREADABLE' if kind == 'UNREADABLE' else 'wrong-release'}" in result.stderr
    updates = [c for c in recorded if c[:3] == ["aws", "ecs", "update-service"]]
    assert [c[c.index("--region") + 1] for c in updates] == (
        ["eu-west-1", "eu-west-1"] if region == "eu-west-1" else ["eu-west-1", "eu-west-3", "eu-west-3"]
    )
    assert [c[c.index("--region") + 1] for c in updates if c[c.index("--task-definition") + 1] == "old:19"] == [region]
    assert f"rollback stabilized in {region}" in result.stderr
    assert (tmp_path / "unlock").read_text().strip() == str(result.returncode)


@pytest.mark.parametrize("failures", [1, 10000])
def test_ecs_rollback_verification_retries_with_a_budget(tmp_path: Path, failures: int) -> None:
    result, recorded = run_ecs_fixture(tmp_path, failure="eu-west-1", extra_env={
        "ECS_ROLLBACK_READER_UNREADABLE": f"aws-region:eu-west-1={failures}",
        "TR_ECS_VERIFY_TIMEOUT_SECONDS": "30", "TR_ECS_VERIFY_INTERVAL_SECONDS": "15",
    })
    assert result.returncode != 0
    assert recorded.count(["reader", "aws-region:eu-west-1", "rollback"]) == 2
    assert [c for c in recorded if c[0] == "sleep"] == [["sleep", "15"]]
    assert ("rollback stabilized" in result.stderr) == (failures == 1)
    assert ("needs operator attention" in result.stderr) == (failures != 1)
    assert (tmp_path / "unlock").read_text().strip() == str(result.returncode)


def test_ecs_final_verification_exhaustion_does_not_roll_back(tmp_path: Path) -> None:
    result, recorded = run_ecs_fixture(tmp_path, extra_env={
        "ECS_READER_WRONG": "aws=10000",
        "TR_ECS_VERIFY_TIMEOUT_SECONDS": "30", "TR_ECS_VERIFY_INTERVAL_SECONDS": "15",
    })
    assert result.returncode != 0
    assert "last observed=wrong-release" in result.stderr
    assert "final fleet verification failed" in result.stderr
    assert recorded.count(["reader", "aws", "rollout"]) == 2
    updates = [c for c in recorded if c[:3] == ["aws", "ecs", "update-service"]]
    assert len(updates) == 2
    assert all(c[c.index("--task-definition") + 1] != "old:19" for c in updates)
    assert (tmp_path / "unlock").read_text().strip() == str(result.returncode)


@pytest.mark.parametrize("setting", ["TR_ECS_VERIFY_TIMEOUT_SECONDS", "TR_ECS_VERIFY_INTERVAL_SECONDS"])
@pytest.mark.parametrize("value", ["0", "abc", "", "-1", "1.5"])
def test_ecs_verification_refuses_invalid_timing_before_registration(
    tmp_path: Path, setting: str, value: str,
) -> None:
    result, recorded = run_ecs_fixture(tmp_path, extra_env={setting: value})
    assert result.returncode != 0
    assert f"{setting} must be a positive integer" in result.stderr
    assert not any(c[:3] == ["aws", "ecs", "register-task-definition"] for c in recorded)
    assert not any(c[:3] == ["aws", "ecs", "update-service"] for c in recorded)


@pytest.mark.parametrize("verifier_rc", [0, 1, 5])
def test_ecs_completeness_exit_code_is_preserved(tmp_path: Path, verifier_rc: int) -> None:
    result, recorded = run_ecs_fixture(tmp_path, verifier_rc=verifier_rc)
    assert result.returncode == verifier_rc, result.stderr
    assert ["verify_cloud_complete.sh", "aws"] in recorded
    updates = [c for c in recorded if c[:3] == ["aws", "ecs", "update-service"]]
    assert len(updates) == 2
    assert all(c[c.index("--task-definition") + 1] != "old:19" for c in updates)
    assert (tmp_path / "unlock").read_text().strip() == str(result.returncode)


@pytest.mark.parametrize("region", ["eu-west-1", "eu-west-3"])
@pytest.mark.parametrize("live_outbox", [None, "", "TRUE"])
def test_ecs_rollout_refuses_invalid_outbox_without_rolling_back_healthy_region(
    tmp_path: Path, region: str, live_outbox: str | None,
) -> None:
    definitions = {name: task_definition(name) for name in ("eu-west-1", "eu-west-3")}
    container = definitions[region]["taskDefinition"]["containerDefinitions"][0]
    if live_outbox is None:
        container["environment"].pop(2)
    else:
        container["environment"][2]["value"] = live_outbox
    result, recorded = run_ecs_fixture(tmp_path, definitions=definitions)
    assert result.returncode != 0
    assert "Cannot safely clone the ECS workload; no runtime configuration printed" in result.stderr
    # The refusal names the setting (never a value) so the CI log explains itself.
    assert "TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED" in result.stderr
    assert (tmp_path / "unlock").read_text().strip() == str(result.returncode)
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


def test_ecs_rollout_registers_untagged_live_definitions(tmp_path: Path) -> None:
    # Production shape: neither live definition carries tags. The rollout must
    # register both regions anyway (the fake API refuses an empty tags list).
    definitions = {region: task_definition(region) for region in ("eu-west-1", "eu-west-3")}
    for definition in definitions.values():
        definition.pop("tags")
    result, recorded = run_ecs_fixture(tmp_path, definitions=definitions)
    assert result.returncode == 0, result.stderr
    updates = [c for c in recorded if c[:3] == ["aws", "ecs", "update-service"]]
    assert [c[c.index("--region") + 1] for c in updates] == ["eu-west-1", "eu-west-3"]


def test_ecs_rollout_enables_the_legacy_ireland_outbox(tmp_path: Path) -> None:
    definitions = {region: task_definition(region) for region in ("eu-west-1", "eu-west-3")}
    definitions["eu-west-1"]["taskDefinition"]["containerDefinitions"][0]["environment"][2]["value"] = "false"
    result, recorded = run_ecs_fixture(tmp_path, definitions=definitions)
    assert result.returncode == 0, result.stderr
    assert_registrations_preserve_regional_configuration(tmp_path)
    assert ["verify_cloud_complete.sh", "aws"] in recorded


@pytest.mark.parametrize("allow_rollback", [False, True])
def test_ecs_record_read_rollback_requires_explicit_override(tmp_path: Path, allow_rollback: bool) -> None:
    definitions = {region: task_definition(region) for region in ("eu-west-1", "eu-west-3")}
    paris = definitions["eu-west-3"]["taskDefinition"]["containerDefinitions"][0]
    paris["environment"].append({"name": "TR_RECORD_READ_BACKEND", "value": "clickhouse"})
    for entry in paris["environment"]:
        if entry["name"] == "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_USER":
            entry["value"] = "tr_control_read"
    paris["secrets"][-1]["valueFrom"] = READER_ARN
    result, calls = run_ecs_fixture(tmp_path, definitions=definitions, extra_env={
        "TR_RECORD_READ_BACKEND_EU_WEST_3": "postgres",
        "TR_ECS_ALLOW_RECORD_READ_ROLLBACK": str(allow_rollback).lower(),
    })
    assert (result.returncode == 0) == allow_rollback, result.stderr
    registrations = dict(json.loads(line) for line in (tmp_path / "registrations").read_text().splitlines())
    assert not any(c[:2] == ["aws", "secretsmanager"] for c in calls)
    if allow_rollback:
        updated = registrations["eu-west-3"]["containerDefinitions"][0]
        expected = copy.deepcopy(paris)
        expected["image"] = updated["image"]
        for entry in expected["environment"]:
            if entry["name"] in {"TR_RELEASE", "RELEASE_COMMIT"}:
                entry["value"] = "c" * 40
            elif entry["name"] == "TR_RECORD_READ_BACKEND":
                entry["value"] = "postgres"
        expected["environment"].append({"name": "TR_REMEDIATOR_IN_PROCESS_ENABLED", "value": "false"})
        assert updated == expected
    else:
        assert "TR_ECS_ALLOW_RECORD_READ_ROLLBACK=true" in result.stderr
        assert list(registrations) == ["eu-west-1"]


@pytest.mark.parametrize("fault", ["no_url", "lookup_failed", "missing_arn"])
def test_ecs_record_reads_refuse_incomplete_reader_configuration(tmp_path: Path, fault: str) -> None:
    definitions = {region: task_definition(region) for region in ("eu-west-1", "eu-west-3")}
    extra_env = {}
    if fault == "no_url":
        paris = definitions["eu-west-3"]["taskDefinition"]["containerDefinitions"][0]
        paris["environment"] = [e for e in paris["environment"] if e["name"] != "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL"]
    elif fault == "lookup_failed":
        extra_env["ECS_READER_LOOKUP_FAILS"] = "1"
    else:
        extra_env["ECS_READER_ARN"] = "None"
    result, calls = run_ecs_fixture(tmp_path, definitions=definitions, extra_env=extra_env)
    assert result.returncode != 0
    mutations = [c for c in calls if c[:3] in (
        ["aws", "ecs", "register-task-definition"], ["aws", "ecs", "update-service"],
    )]
    assert [(c[2], c[c.index("--region") + 1]) for c in mutations] == [
        ("register-task-definition", "eu-west-1"), ("update-service", "eu-west-1"),
    ]


def test_ecs_ireland_clickhouse_override_requires_its_own_url(tmp_path: Path) -> None:
    result, calls = run_ecs_fixture(tmp_path, extra_env={"TR_RECORD_READ_BACKEND_EU_WEST_1": "clickhouse"})
    assert result.returncode != 0
    assert "cloned TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_URL" in result.stderr
    assert not any(c[:3] == ["aws", "ecs", "register-task-definition"] for c in calls)


@pytest.mark.parametrize(("full_table", "short_table"), [
    ("REGIONS=(eu-west-1 eu-west-3)", "REGIONS=(eu-west-1)"),
    ("SERVICES=(tr-cp-euw1 tr-cp-euw3)", "SERVICES=(tr-cp-euw1)"),
    ('EXPECTED_OUTBOX=("$TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED" "$TR_OPERATIONAL_ANALYTICS_OUTBOX_ENABLED")', "EXPECTED_OUTBOX=(true)"),
    ('EXPECTED_RECORD_READ_BACKEND=("${TR_RECORD_READ_BACKEND_EU_WEST_1:-postgres}" "${TR_RECORD_READ_BACKEND_EU_WEST_3:-postgres}")', "EXPECTED_RECORD_READ_BACKEND=(postgres)"),
])
def test_ecs_rollout_refuses_misaligned_tables_before_registration(
    tmp_path: Path, full_table: str, short_table: str,
) -> None:
    source_root = tmp_path / "source"
    scripts = source_root / "scripts/deploy"
    scripts.mkdir(parents=True)
    for name in ECS_SCRIPT_FILES:
        shutil.copy2(ROOT / "scripts/deploy" / name, scripts / name)
    script = scripts / "aws_ecs_control_plane.sh"
    text = script.read_text()
    assert text.count(full_table) == 1  # a renamed table must fail here, not pass by doing nothing
    script.write_text(text.replace(full_table, short_table))
    result, recorded = run_ecs_fixture(tmp_path / "run", source_root=source_root)
    assert result.returncode != 0
    assert "REGIONS, SERVICES, EXPECTED_OUTBOX, and EXPECTED_RECORD_READ_BACKEND must have the same length" in result.stderr
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


def test_paris_ships_on_postgres_until_the_grant() -> None:
    """A Paris task referencing the reader secret before tr-cp-exec may read
    it fails to start, and the release updates Ireland first, so Paris's
    default moves to clickhouse only in a change after that grant
    (infra/aws_ecs_control_reader.tf) has applied."""
    script = (ROOT / "scripts/deploy/aws_ecs_control_plane.sh").read_text()
    assert (
        'EXPECTED_RECORD_READ_BACKEND=("${TR_RECORD_READ_BACKEND_EU_WEST_1:-postgres}" '
        '"${TR_RECORD_READ_BACKEND_EU_WEST_3:-postgres}")'
    ) in script


@pytest.mark.parametrize("database", [None, "tr", "fixture-database"])
def test_clickhouse_record_reads_refuse_a_database_the_reader_cannot_read(database: str | None) -> None:
    """The reader is granted SELECT on default.* only: a cloned task that reads
    any other database would answer every record read with an access error."""
    payload = task_definition("eu-west-3")
    env = payload["taskDefinition"]["containerDefinitions"][0]["environment"]
    env[:] = [entry for entry in env if entry["name"] != "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE"]
    if database is not None:
        env.append({"name": "TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE", "value": database})
    with pytest.raises(ValueError, match="TR_OPERATIONAL_ANALYTICS_CLICKHOUSE_DATABASE=default"):
        load_builder()(payload, "registry@sha256:" + "b" * 64, "c" * 40, "true", "clickhouse", READER_ARN)
