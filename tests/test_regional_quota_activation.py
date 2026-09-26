"""Execute issuance interlocks with recorded cloud replies, without cloud access."""
from __future__ import annotations

import copy
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from .deploy_script_harness import (
    QUOTA_EXECUTION,
    QUOTA_SCHEDULER,
    QUOTA_WORKER,
    DeployScriptHarness,
    summarise,
)
from .test_deploy_script_execution import _run_regional_quota_reconciler

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/deploy/regional_quota_rollout.sh"
ROLLOUT = ROOT / "scripts/deploy/rollout.sh"
WORKFLOW = ROOT / ".github/workflows/deploy.yml"


def _run(tmp_path: Path, body: str, *, replies: dict[str, Any] | None = None) -> subprocess.CompletedProcess[str]:
    for name, value in (replies or {}).items():
        (tmp_path / f"{name}.json").write_text(json.dumps(value))
    if replies is not None:
        replies.setdefault("executions", [replies["execution"]] if "execution" in replies else [])
        (tmp_path / "executions.json").write_text(json.dumps(replies["executions"]))
    script = r'''
set -euo pipefail
source "$HELPER"
PROJECT_ID=quill-cloud-proxy
RUN_SERVICE_ACCOUNT=44325983244-compute@developer.gserviceaccount.com
TR_PRIMARY_REGION=us-central1
TR_CONTROL_PLANE_REGIONS=us-central1,us-east4,europe-west4,southamerica-east1
TR_DEPLOY_RELEASE_ID=new-release
# Load the real shared constant without _lib cloud/config initialization.
eval "$(sed -n '/^REGIONAL_QUOTA_ACCOUNTING_PROTOCOL=/p' "$LIB")"
# Load only the pure image helpers, without cloud initialization.
eval "$(sed -n '/^regional_quota_resolve_image()/,/^# R4:/p' "$LIB" | sed '$d')"
IMAGE_ACCOUNTING_PROTOCOL=2
SERVICE=trusted-router
GITHUB_REPOSITORY=Lore-Hex/quill-router
GITHUB_REF_NAME=main
HOTFIX=false
gh() { printf '%s\n' "$*" >> "$FIXTURES/queued-deploy"; }
log() { echo "$*" >&2; }
gc() {
  echo "$*" >> "$FIXTURES/calls"
  case "$1 $2 $3" in
    'run services describe') echo '{"status":{"traffic":[{"percent":100,"revisionName":"active"}]}}' ;;
    'run revisions describe')
      if [[ "$*" == *--region=us-east4* ]]; then cat "$FIXTURES/secondary.json"
      else cat "$FIXTURES/revision.json"; fi ;;
    'scheduler jobs describe') cat "$FIXTURES/scheduler.json" ;;
    'run jobs describe') cat "$FIXTURES/worker.json" ;;
    'run jobs executions') if [ "$4" = list ]; then cat "$FIXTURES/executions.json"; else cat "$FIXTURES/execution.json"; fi ;;
    'logging read '*) cat "$FIXTURES/evidence.json" ;;
    'storage buckets describe')
      if [ -f "$FIXTURES/lifecycle.json" ]; then cat "$FIXTURES/lifecycle.json"
      else echo '{"lifecycle_config":{"rule":[{"action":{"type":"Delete"},"condition":{"age":1,"matchesPrefix":["locks/"]}}]}}'; fi ;;
    'storage objects list')
      if [ -f "$FIXTURES/list-error" ]; then cat "$FIXTURES/list-error" >&2; return 1; fi
      if [ -f "$FIXTURES/list-raw" ]; then cat "$FIXTURES/list-raw"
      elif [ -f "$FIXTURES/latch" ]; then
        echo '[{"bucket":"tr-deploy-mutex-quill-cloud-proxy","name":"controls/regional-quota-issuance.txt"}]'
      else echo '[]'; fi ;;
    'storage cat '*)
      if [ -f "$FIXTURES/latch-error" ]; then
        cat "$FIXTURES/latch-error" >&2; return 1
      elif [ -f "$FIXTURES/latch" ]; then cat "$FIXTURES/latch"
      else
        echo 'ERROR: (gcloud.storage.cat) One or more URLs matched no objects.' >&2
        return 1
      fi ;;

    'storage cp '*) cp "$3" "$FIXTURES/latch" ;;
    *) echo "unexpected cloud call: $*" >&2; return 90 ;;
  esac
}
'''
    return subprocess.run(  # noqa: S603 - fixed shell, repository-owned code, local fake gc
        ["/bin/bash", "-c", script + body], text=True, capture_output=True,
        env={**os.environ, "HELPER": str(HELPER), "LIB": str(ROOT / "scripts/deploy/_lib.sh"), "FIXTURES": str(tmp_path)}, check=False,
    )


def _replies() -> dict[str, Any]:
    revision = {"spec": {"containers": [{"env": [
        {"name": "TR_RELEASE", "value": "older-release"},
        {"name": "REGIONAL_QUOTA_ACCOUNTING_PROTOCOL", "value": "2"},
        {"name": "TR_REGIONAL_QUOTA_LEASES_ENABLED", "value": "true"},
        {"name": "TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED", "value": "false"},
    ]}]}}
    replies: dict[str, Any] = {name: copy.deepcopy(value) for name, value in {
        "revision": revision, "secondary": revision, "scheduler": QUOTA_SCHEDULER,
        "worker": QUOTA_WORKER, "execution": QUOTA_EXECUTION,
        "evidence": [{"textPayload": "regional_quota.reconciler_complete elapsed_ms=10"}],
    }.items()}
    replies["secondary"] = copy.deepcopy(revision)
    replies["execution"]["status"]["completionTime"] = datetime.now(UTC).isoformat()
    return replies


def _activation_body() -> str:
    # Execute the actual rollout call site: deleting either gate must go red.
    source = ROLLOUT.read_text()
    start = source.index('if [ "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" = "true" ]; then')
    end = source.index('\nfi', start) + len('\nfi')
    return 'REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED=true\n' + source[start:end]


def test_issuance_accepts_verified_fleet_and_worker(tmp_path: Path) -> None:
    run = _run(tmp_path, _activation_body(), replies=_replies())
    assert run.returncode == 0, run.stderr
    calls = (tmp_path / "calls").read_text()
    assert calls.count("run revisions describe") == 4
    assert "logging read" in calls
    assert not any(word in calls for word in (" resume ", " pause ", " execute "))


@pytest.mark.parametrize("target", ["secondary", "worker"])
@pytest.mark.parametrize("protocol", [None, "1"])
def test_issuance_rejects_incompatible_protocol(tmp_path: Path, target: str, protocol: str | None) -> None:
    replies = _replies()
    spec = replies[target]["spec"]
    if target == "worker":
        spec = spec["template"]["spec"]["template"]["spec"]
    env = spec["containers"][0]["env"]
    env[:] = [item for item in env if item["name"] != "REGIONAL_QUOTA_ACCOUNTING_PROTOCOL"]
    if protocol is not None:
        env.append({"name": "REGIONAL_QUOTA_ACCOUNTING_PROTOCOL", "value": protocol})
    if target == "worker":
        replies["execution"]["spec"]["template"]["spec"] = copy.deepcopy(spec)
    run = _run(tmp_path, _activation_body(), replies=replies)
    assert run.returncode != 0, run.stdout + run.stderr
    assert f"incompatible accounting protocol {protocol or 'missing'}" in run.stderr


@pytest.mark.parametrize("fault", [
    "paused", "absent-schedule", "wrong-target", "absent-worker", "unready",
    "failed", "stale", "old-execution", "skipped",
])
def test_issuance_rejects_unhealthy_reconciler(tmp_path: Path, fault: str) -> None:
    replies = _replies()
    if fault == "paused":
        replies["scheduler"]["state"] = "PAUSED"
    elif fault == "absent-schedule":
        del replies["scheduler"]
    elif fault == "wrong-target":
        replies["scheduler"]["httpTarget"]["uri"] = "https://unrelated.invalid/worker"
    elif fault == "absent-worker":
        del replies["worker"]
    elif fault == "unready":
        replies["worker"]["status"]["conditions"][0]["status"] = "False"
    elif fault == "failed":
        replies["worker"]["status"]["latestCreatedExecution"]["completionStatus"] = "EXECUTION_FAILED"
        replies["execution"]["status"]["conditions"][0]["status"] = "False"
    elif fault == "stale":
        replies["execution"]["status"]["completionTime"] = "2020-01-01T00:00:00Z"
    elif fault == "old-execution":
        replies["execution"]["spec"]["template"]["spec"]["containers"][0]["image"] = "old-image"
    else:
        replies["evidence"] = []
    run = _run(tmp_path, _activation_body(), replies=replies)
    assert run.returncode != 0, run.stdout + run.stderr
    assert "refusing regional quota issuance" in run.stderr
    if fault == "paused":
        assert "reconciler schedule is not ENABLED" in run.stderr
    if fault in ("absent-schedule", "absent-worker"):
        assert "cannot read reconciler" in run.stderr
    calls = (tmp_path / "calls").read_text()
    assert not any(word in calls for word in (" resume ", " pause ", " execute ", " deploy "))


@pytest.mark.parametrize(("requested", "hotfix"), [
    ("true", "false"), ("preserve", "true"), ("", "false"),
])
def test_pending_off_survives_newer_push(tmp_path: Path, requested: str, hotfix: str) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    # A runs in the ordinary group; B (OFF) must not share C's pending slot.
    assert workflow["concurrency"]["group"] == (
        "${{ inputs.regional_quota_lease_issuance == 'false' && "
        "format('regional-quota-off-{0}', github.run_id) || 'deploy-trusted-router' }}"
    )
    job = workflow["jobs"]["persist-regional-quota-stop"]
    assert "needs" not in job
    for dependent in ("gate-on-ci", "build-image"):
        assert "persist-regional-quota-stop" in workflow["jobs"][dependent]["needs"]
        assert "inputs.regional_quota_lease_issuance != 'false'" in workflow["jobs"][dependent]["if"]
    step = next(s for s in job["steps"] if "run" in s)
    assert step["if"] == ("github.event_name == 'workflow_dispatch' && "
                          "inputs.regional_quota_lease_issuance == 'false'")
    # A is actively armed. Losing B must not leave that permission in place.
    (tmp_path / "latch").write_text("allow\n")
    # Run B's real persistence step, then discard B's deploy. Only _lib setup
    # is omitted: the fake gc and fixed project above replace that environment.
    body = step["run"].replace("source scripts/deploy/_lib.sh", ":")
    body = body.replace("source scripts/deploy/regional_quota_rollout.sh", 'source "$HELPER"')
    run = _run(tmp_path, f"HOTFIX={hotfix}\n" + body)
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "latch").read_text().strip() == "off"
    assert (tmp_path / "queued-deploy").read_text().strip() == (
        "workflow run deploy.yml --repo Lore-Hex/quill-router --ref main "
        f"-f regional_quota_lease_issuance=preserve -f hotfix={hotfix}"
    )
    # C arrives later; execute the actual rollout resolution, even with a
    # future ON pin and a primary still serving ON. B need never deploy.
    source = ROLLOUT.read_text()
    start = source.index("REGIONAL_QUOTA_LEASE_ISSUANCE_PINNED=")
    end = source.index('if [ "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" = "true" ] &&', start)
    resolution = source[start:end]
    body = f"TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED={requested}\n"
    body += 'read_primary_regional_quota_env() { printf "true\\n"; }\n'
    body += resolution + '\nprintf "%s\\n" "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"\n'
    run = _run(tmp_path, body)
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "false"



def _resolution_body(requested: str) -> str:
    source = ROLLOUT.read_text()
    start = source.index("REGIONAL_QUOTA_LEASE_ISSUANCE_PINNED=")
    end = source.index('if [ "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED" = "true" ] &&', start)
    return (f"TR_REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED={requested}\n"
            + 'read_primary_regional_quota_env() { printf "true\\n"; }\n'
            + source[start:end]
            + '\nprintf "%s\\n" "$REGIONAL_QUOTA_LEASE_ISSUANCE_ENABLED"\n')


def test_issuance_pin_is_true() -> None:
    assert "\nREGIONAL_QUOTA_LEASE_ISSUANCE_PINNED=true\n" in ROLLOUT.read_text()


@pytest.mark.parametrize("requested", ["true", "false"])
@pytest.mark.parametrize("latch", [None, "garbage", "", "off", "allow"])
def test_latch_truth_table(tmp_path: Path, latch: str | None, requested: str) -> None:
    if latch is not None:
        (tmp_path / "latch").write_text(latch)
    run = _run(tmp_path, _resolution_body(requested))
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == (requested if latch in (None, "allow") else "false")
    if latch in ("garbage", ""):
        assert "invalid content; treating as off" in run.stderr


@pytest.mark.parametrize("requested", ["true", "false"])
@pytest.mark.parametrize("error", [
    "ERROR: (gcloud.storage.cat) HTTPError 403: Permission denied",
    "ERROR: (gcloud.storage.cat) Connection reset by peer",
    "ERROR: (gcloud.storage.cat) HTTPError 404: The specified bucket does not exist.",
])
def test_latch_read_error_aborts_rollout(tmp_path: Path, requested: str, error: str) -> None:
    (tmp_path / "latch").write_text("off")
    (tmp_path / "latch-error").write_text(error)
    run = _run(tmp_path, _resolution_body(requested))
    assert run.returncode != 0
    assert "refusing regional quota rollout: cannot read stop latch" in run.stderr
    assert error in run.stderr
    assert not run.stdout.strip()


@pytest.mark.parametrize("error", [
    "ERROR: (gcloud.storage.cat) The following URLs matched no objects or files:\ngs://tr-deploy-mutex-quill-cloud-proxy/controls/regional-quota-issuance.txt",
    "ERROR: (gcloud.storage.cat) No URLs matched: gs://tr-deploy-mutex-quill-cloud-proxy/controls/regional-quota-issuance.txt",
    "ERROR: (gcloud.storage.cat) HTTPError 404: No such object: tr-deploy-mutex-quill-cloud-proxy/controls/regional-quota-issuance.txt",
])
def test_latch_object_not_found_preserves_request(tmp_path: Path, error: str) -> None:
    (tmp_path / "latch-error").write_text(error)
    run = _run(tmp_path, _resolution_body("true"))
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "true"
    assert "storage cat" not in (tmp_path / "calls").read_text()


@pytest.mark.parametrize(("event", "control", "persists"), [
    ("push", "", False), ("push", "false", False),
    ("workflow_dispatch", "preserve", False),
    ("workflow_dispatch", "true", False), ("workflow_dispatch", "false", True),
])
def test_only_off_dispatch_persists_stop(tmp_path: Path, event: str, control: str, persists: bool) -> None:
    workflow = yaml.safe_load(WORKFLOW.read_text())
    job = workflow["jobs"]["persist-regional-quota-stop"]
    step = next(s for s in job["steps"] if "run" in s)
    condition = step["if"].replace("github.event_name", repr(event)).replace(
        "inputs.regional_quota_lease_issuance", repr(control),
    ).replace("&&", "and")
    enabled = eval(condition, {"__builtins__": {}}, {})  # noqa: S307 - repository-owned boolean expression
    body = step["run"].replace("source scripts/deploy/_lib.sh", ":").replace(
        "source scripts/deploy/regional_quota_rollout.sh", 'source "$HELPER"',
    )
    run = _run(tmp_path, body if enabled else ":")
    assert run.returncode == 0, run.stderr
    assert (tmp_path / "latch").exists() is persists
    assert (tmp_path / "queued-deploy").exists() is persists


@pytest.mark.parametrize("script", ["rollout.sh", "worker-create", "worker-update"])
def test_protocol_marker_is_set_on_deployed_containers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str,
) -> None:
    # A deliberately different, compatible label proves the checkout constant
    # cannot manufacture the marker. Assert recorded argv of the real scripts.
    env = {"HARNESS_IMAGE_CONFIG": json.dumps({"config": {"Labels": {
        "com.trustedrouter.accounting_protocol": "3",
    }}})}
    if script == "rollout.sh":
        run = DeployScriptHarness(tmp_path / "serving").run("scripts/deploy/rollout.sh", extra_env=env)
        calls = [c for c in run.calls if "run" in c and "deploy" in c]
    else:
        run = _run_regional_quota_reconciler(
            tmp_path, monkeypatch, state="PAUSED",
            versioned_job_exists=script == "worker-update", extra_env=env,
        )
        calls = [c for c in run.calls if "run" in c and "jobs" in c and ("create" in c or "update" in c)]
    assert run.returncode == 0, summarise(run)
    assert calls
    for call in calls:
        flag = "--set-env-vars" if "--set-env-vars" in call else "--update-env-vars"
        assert "REGIONAL_QUOTA_ACCOUNTING_PROTOCOL=3" in call[call.index(flag) + 1].split("|")
        assert "@sha256:" in call[call.index("--image") + 1]


@pytest.mark.parametrize("prefixes", [None, [], ["controls/"], ["con"], ["controls/child"], ["locks/", "controls/"]])
@pytest.mark.parametrize("operation", ["regional_quota_apply_stop_latch true", "regional_quota_persist_stop"])
def test_lifecycle_cannot_expire_stop(tmp_path: Path, prefixes: list[str] | None, operation: str) -> None:
    condition: dict[str, Any] = {"age": 1}
    if prefixes is not None:
        condition["matchesPrefix"] = prefixes
    (tmp_path / "latch").write_text("off")
    run = _run(tmp_path, operation, replies={"lifecycle": {"lifecycle_config": {
        "rule": [{"action": {"type": "Delete"}, "condition": condition}],
    }}})
    assert run.returncode != 0
    assert "unsafe or unreadable control bucket lifecycle" in run.stderr
    assert "storage cat" not in (tmp_path / "calls").read_text()
    assert "storage cp" not in (tmp_path / "calls").read_text()


def test_restricted_lifecycle_preserves_off(tmp_path: Path) -> None:
    # Execute the provisioning block and inspect the policy file and IAM argv
    # actually sent to gcloud, rather than matching an unused source string.
    source = (ROOT / "scripts/deploy/infra.sh").read_text()
    start = source.index('log "ensuring production deployment mutex bucket"')
    end = source.index('log "ensuring BYOK envelope KMS key"', start)
    body = r'''DEPLOY_SERVICE_ACCOUNT=tr-deploy@example.invalid
    gc() {
      echo "$*" >> "$FIXTURES/infra-calls"
      for arg in "$@"; do
        case "$arg" in
          --lifecycle-file=*) cp "${arg#*=}" "$FIXTURES/emitted-policy.json" ;;
        esac
      done
    }
''' + source[start:end]
    provision = _run(tmp_path, body)
    assert provision.returncode == 0, provision.stderr
    policy = json.loads((tmp_path / "emitted-policy.json").read_text())
    assert policy["rule"][0]["condition"].get("matchesPrefix") == ["locks/"]
    assert "--role=roles/storage.legacyBucketReader" in (tmp_path / "infra-calls").read_text()
    (tmp_path / "latch").write_text("off")
    run = _run(tmp_path, _resolution_body("true"), replies={"lifecycle": {"lifecycle_config": policy}})
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == "false"


@pytest.mark.parametrize("fault", ["permission", "transport", "missing-bucket", "malformed", "wrong-shape", "missing-gc"])
def test_listing_failures_abort(tmp_path: Path, fault: str) -> None:
    body = _resolution_body("true")
    if fault == "malformed":
        (tmp_path / "list-raw").write_text("not-json")
    elif fault == "wrong-shape":
        (tmp_path / "list-raw").write_text('{}')
    elif fault == "missing-gc":
        body = "unset -f gc\n" + body
    else:
        (tmp_path / "list-error").write_text(fault)
    run = _run(tmp_path, body)
    assert run.returncode != 0
    assert "refusing regional quota rollout" in run.stderr
    assert not run.stdout.strip()


@pytest.mark.parametrize("state", ["EXECUTION_RUNNING", "EXECUTION_PENDING"])
def test_healthy_reconciliation_overlap(tmp_path: Path, state: str) -> None:
    replies = _replies()
    replies["worker"]["status"]["latestCreatedExecution"]["completionStatus"] = state
    replies["execution"]["metadata"] = {"name": "quota-execution"}
    replies["executions"] = [replies["execution"]]
    run = _run(tmp_path, _activation_body(), replies=replies)
    assert run.returncode == 0, run.stderr
    assert "executions list" in (tmp_path / "calls").read_text()
    assert "logging read" in (tmp_path / "calls").read_text()


@pytest.mark.parametrize("override", ["prefix", "name"])
def test_custom_worker_name(tmp_path: Path, override: str) -> None:
    replies = _replies()
    job = "custom-worker-abc12345"
    replies["scheduler"]["httpTarget"]["uri"] = replies["scheduler"]["httpTarget"]["uri"].replace(
        "trusted-router-regional-quota-reconciler-abc12345", job,
    )
    setting = "TR_REGIONAL_QUOTA_RECONCILER_JOB_PREFIX=custom-worker" if override == "prefix" else f"TR_REGIONAL_QUOTA_RECONCILER_JOB={job}"
    run = _run(tmp_path, setting + "\n" + _activation_body(), replies=replies)
    assert run.returncode == 0, run.stderr
    assert f"run jobs describe {job}" in (tmp_path / "calls").read_text()


@pytest.mark.parametrize("script", ["rollout.sh", "worker"])
@pytest.mark.parametrize("config", [{"config": {}}, {"config": {"Labels": {"com.trustedrouter.accounting_protocol": "1"}}}])
def test_overridden_incompatible_image_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, script: str, config: dict[str, Any],
) -> None:
    env = {"IMAGE": "us-central1-docker.pkg.dev/project/repo/old:pre-r1",
           "HARNESS_IMAGE_CONFIG": json.dumps(config)}
    if script == "rollout.sh":
        run = DeployScriptHarness(tmp_path / "serving").run("scripts/deploy/rollout.sh", extra_env=env)
    else:
        run = _run_regional_quota_reconciler(tmp_path, monkeypatch, state="PAUSED", extra_env=env)
    assert run.returncode != 0
    assert "protocol" in run.stderr
    assert not any("--image" in c and ("deploy" in c or "create" in c or "update" in c) for c in run.calls)


@pytest.mark.parametrize("eventually_succeeds", [True, False])
def test_inflight_wait_is_bounded(tmp_path: Path, eventually_succeeds: bool) -> None:
    replies = _replies()
    replies["worker"]["status"]["latestCreatedExecution"]["completionStatus"] = "EXECUTION_PENDING"
    replies["execution"]["metadata"] = {"name": "quota-execution"}
    replies["executions"] = []
    replies["after"] = [replies["execution"]] if eventually_succeeds else []
    body = '''sleep() {
      echo "$*" >> "$FIXTURES/sleeps"
      cp "$FIXTURES/after.json" "$FIXTURES/executions.json"
    }
''' + _activation_body()
    run = _run(tmp_path, body, replies=replies)
    assert (run.returncode == 0) is eventually_succeeds, run.stderr
    assert (tmp_path / "sleeps").is_file()
    sleeps = (tmp_path / "sleeps").read_text().splitlines()
    assert sleeps == ["10"] * (1 if eventually_succeeds else 9)
    if not eventually_succeeds:
        assert "after bounded wait" in run.stderr


@pytest.mark.parametrize("fault", ["failed", "stale", "old-config", "no-evidence"])
def test_overlap_does_not_weaken_success_evidence(tmp_path: Path, fault: str) -> None:
    replies = _replies()
    replies["worker"]["status"]["latestCreatedExecution"]["completionStatus"] = "EXECUTION_RUNNING"
    replies["execution"]["metadata"] = {"name": "quota-execution"}
    if fault == "failed":
        replies["execution"]["status"]["conditions"][0]["status"] = "False"
    elif fault == "stale":
        replies["execution"]["status"]["completionTime"] = "2020-01-01T00:00:00Z"
    elif fault == "old-config":
        replies["execution"]["spec"]["template"]["spec"]["containers"][0]["image"] = "old"
    else:
        replies["evidence"] = []
    replies["executions"] = [replies["execution"]]
    run = _run(tmp_path, 'sleep() { :; }\n' + _activation_body(), replies=replies)
    assert run.returncode != 0
    assert "refusing regional quota issuance" in run.stderr


def test_latch_read_deadline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Shorten the real deadline only in the isolated test copy.
    helper = tmp_path / "helper.sh"
    helper.write_text(HELPER.read_text().replace("timeout=min(20, float(sys.argv[2]))", "timeout=0.05"))
    monkeypatch.setattr(f"{__name__}.HELPER", helper)
    run = _run(tmp_path, 'gc() { /bin/sleep 5; }\n' + _resolution_body("true"))
    assert run.returncode != 0
    assert "command exceeded 20 seconds" in run.stderr
    assert not run.stdout.strip()


@pytest.mark.parametrize("config", [
    {}, {"config": {"Labels": {"com.trustedrouter.accounting_protocol": "garbage"}}},
    {"config": {"Labels": {"com.trustedrouter.accounting_protocol": 2}}},
    {"linux/amd64": {"config": {"Labels": {"com.trustedrouter.accounting_protocol": "3"}}}},
])
def test_image_metadata_is_read_from_selected_digest(tmp_path: Path, config: dict[str, Any]) -> None:
    body = '''IMAGE=registry.invalid/project/old:override
    gc() { printf 'sha256:%064d\\n' 7; }
    gcloud() { :; }
    docker() { echo "$*" > "$FIXTURES/image-call"; cat "$FIXTURES/config.json"; }
    regional_quota_resolve_image
    printf '%s\\n' "$IMAGE_ACCOUNTING_PROTOCOL"
'''
    run = _run(tmp_path, body, replies={"config": config})
    if "linux/amd64" in config:
        assert run.returncode == 0, run.stderr
        assert run.stdout.strip() == "3"
    else:
        assert run.returncode != 0
    call = (tmp_path / "image-call").read_text()
    assert "old@sha256:" + "0" * 63 + "7" in call
    assert "--format {{json .Image}}" in call
    assert ":override" not in call


def test_build_paths_bake_protocol_label(tmp_path: Path) -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "ARG REGIONAL_QUOTA_ACCOUNTING_PROTOCOL\nLABEL com.trustedrouter.accounting_protocol=${REGIONAL_QUOTA_ACCOUNTING_PROTOCOL}" in dockerfile
    # Execute the real local build command with recording docker, then render
    # and inspect the Cloud Build config/substitutions produced by the workflow.
    body = '''REPO=repo
REGION=us-central1
SERVICE=service
IMAGE=registry.invalid/repo/service:test
    gc() { :; }
    gcloud() { echo "$*" >> "$FIXTURES/gcloud"; if [ "$2" = describe ]; then echo SUCCESS; fi; }
    docker() { echo "$*" >> "$FIXTURES/docker"; }
'''
    local = (ROOT / "scripts/deploy/image.sh").read_text().replace('source "${SCRIPT_DIR}/_lib.sh"', ':')
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(s for s in workflow["jobs"]["build-image"]["steps"] if s.get("name") == "Build image via Cloud Build")
    build = step["run"].replace("source scripts/deploy/_lib.sh", ":").replace(
        "/tmp/cloudbuild-control-plane.yaml", str(tmp_path / "cloudbuild.yaml"),  # noqa: S108 - replace workflow path with isolated fixture
    )
    run = _run(tmp_path, body + local + "\n" + build)
    assert run.returncode == 0, run.stderr
    assert "--build-arg REGIONAL_QUOTA_ACCOUNTING_PROTOCOL=2" in (tmp_path / "docker").read_text()
    cloud = yaml.safe_load((tmp_path / "cloudbuild.yaml").read_text())
    command = cloud["steps"][0]["args"][1]
    calls = (tmp_path / "gcloud").read_text()
    assert "_ACCOUNTING_PROTOCOL=2" in calls
    substitutions = next(arg.split("=", 1)[1] for arg in calls.split() if arg.startswith("--substitutions="))
    # Execute the submitted builder command with its real substitutions, so a
    # build-arg line hidden in an unused shell function cannot satisfy this test.
    assignments = "\n".join(substitutions.split(",")) + "\n"
    run = _run(tmp_path, body + assignments + command)
    assert run.returncode == 0, run.stderr
    builds = [line for line in (tmp_path / "docker").read_text().splitlines()
              if line.startswith("build ")]
    assert len(builds) == 1
    assert "--build-arg REGIONAL_QUOTA_ACCOUNTING_PROTOCOL=2" in builds[0]


def test_custom_prefix_is_used_by_worker_deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_regional_quota_reconciler(
        tmp_path, monkeypatch, state="PAUSED",
        extra_env={"TR_REGIONAL_QUOTA_RECONCILER_JOB_PREFIX": "custom-worker"},
    )
    assert run.returncode == 0, summarise(run)
    calls = [c for c in run.calls if "run" in c and "jobs" in c and "create" in c]
    assert len(calls) == 1
    job = calls[0][calls[0].index("create") + 1]
    assert job.startswith("custom-worker-")
    assert any(f"/jobs/{job}:run" in argument for c in run.calls for argument in c)


@pytest.mark.parametrize("failure", ["unreadable", "invalid-json"])
def test_image_label_read_failure_aborts(tmp_path: Path, failure: str) -> None:
    body = '''IMAGE=registry.invalid/project/old:override
    gc() { printf 'sha256:%064d\\n' 7; }
    gcloud() { :; }
    docker() { ''' + ('return 1;' if failure == "unreadable" else "echo not-json;") + ''' }
    regional_quota_resolve_image
    echo unsafe-success
'''
    run = _run(tmp_path, body)
    assert run.returncode != 0
    assert "unsafe-success" not in run.stdout
    if failure == "unreadable":
        assert "cannot read selected image protocol label" in run.stderr


def test_overlap_skips_successful_single_flight_skip(tmp_path: Path) -> None:
    replies = _replies()
    replies["worker"]["status"]["latestCreatedExecution"]["completionStatus"] = "EXECUTION_RUNNING"
    real = copy.deepcopy(replies["execution"])
    real["metadata"] = {"name": "real-reconciliation"}
    real["status"]["completionTime"] = (datetime.now(UTC) - timedelta(seconds=100)).isoformat()
    skipped = copy.deepcopy(replies["execution"])
    skipped["metadata"] = {"name": "single-flight-skip"}
    replies["executions"] = [skipped, real]
    replies["execution"] = skipped
    # Evidence is execution-specific: a zero exit from the skip cannot qualify.
    body = '''eval "$(declare -f gc | sed '1s/gc/recorded_gc/')"
    export -f recorded_gc
    gc() {
      if [ "$1 $2" = "logging read" ] && [[ "$*" == *single-flight-skip* ]]; then
        echo "$*" >> "$FIXTURES/calls"
        echo '[]'
      else recorded_gc "$@"; fi
    }
''' + _activation_body()
    run = _run(tmp_path, body, replies=replies)
    assert run.returncode == 0, run.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert any("logging read" in c and "real-reconciliation" in c for c in calls)
    assert any("executions list" in c and "--limit=10" in c for c in calls)


@pytest.mark.parametrize("eventually_succeeds", [True, False])
def test_overlap_waits_past_previous_configuration(tmp_path: Path, eventually_succeeds: bool) -> None:
    replies = _replies()
    replies["worker"]["status"]["latestCreatedExecution"]["completionStatus"] = "EXECUTION_PENDING"
    replies["execution"]["metadata"] = {"name": "qualifying-run"}
    old = copy.deepcopy(replies["execution"])
    old["metadata"] = {"name": "previous-configuration"}
    old["spec"]["template"]["spec"]["containers"][0]["image"] = "previous-image"
    replies["executions"] = [old]
    replies["after"] = [replies["execution"], old] if eventually_succeeds else [old]
    body = '''sleep() {
      echo "$*" >> "$FIXTURES/sleeps"
      cp "$FIXTURES/after.json" "$FIXTURES/executions.json"
    }
''' + _activation_body()
    run = _run(tmp_path, body, replies=replies)
    assert (run.returncode == 0) is eventually_succeeds, run.stderr
    assert (tmp_path / "sleeps").read_text().splitlines() == ["10"] * (1 if eventually_succeeds else 9)
    calls = (tmp_path / "calls").read_text().splitlines()
    assert sum("executions list" in c for c in calls) == (2 if eventually_succeeds else 10)
    assert not any("logging read" in c and "previous-configuration" in c for c in calls)
    if not eventually_succeeds:
        assert "after bounded wait" in run.stderr


def test_existing_explicit_worker_outside_prefix_is_updated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run = _run_regional_quota_reconciler(
        tmp_path, monkeypatch, state="PAUSED", versioned_job_exists=True,
        extra_env={"TR_REGIONAL_QUOTA_RECONCILER_JOB": "custom-worker",
                   "HARNESS_VERSIONED_JOB_NAME": "custom-worker"},
    )
    assert run.returncode == 0, summarise(run)
    mutations = [c for c in run.calls if "run" in c and "jobs" in c and ("create" in c or "update" in c)]
    assert len(mutations) == 1
    assert "update" in mutations[0], summarise(run)
    assert mutations[0][mutations[0].index("update") + 1] == "custom-worker"
    assert any("--filter=metadata.name=custom-worker" in c for c in run.calls)


@pytest.mark.parametrize("success_age,accepted", [(100, False), (10, True), (20, False)])
def test_skip_cannot_clear_newer_failure(tmp_path: Path, success_age: int, accepted: bool) -> None:
    replies = _replies()
    replies["worker"]["status"]["latestCreatedExecution"]["completionStatus"] = "EXECUTION_RUNNING"
    now = datetime.now(UTC)
    runs = []
    for name, age, status in [
        ("single-flight-skip", 5, "True"),
        ("failed-reconciliation", 20, "False"),
        ("real-reconciliation", success_age, "True"),
    ]:
        run = copy.deepcopy(replies["execution"])
        run["metadata"]["name"] = name
        run["status"]["completionTime"] = (now - timedelta(seconds=age)).isoformat()
        run["status"]["conditions"][0]["status"] = status
        runs.append(run)
    running = copy.deepcopy(replies["execution"])
    running["status"] = {"conditions": [{"type": "Completed", "status": "Unknown"}]}
    # Deliberately unordered: selection must use completion time, not list order.
    replies["executions"] = [runs[2], running, runs[0], runs[1]]
    body = '''sleep() { echo "$*" >> "$FIXTURES/sleeps"; }
    eval "$(declare -f gc | sed '1s/gc/recorded_gc/')"
    export -f recorded_gc
    gc() {
      if [ "$1 $2" = "logging read" ] && [[ "$*" == *single-flight-skip* ]]; then
        echo "$*" >> "$FIXTURES/calls"
        echo '[]'
      else recorded_gc "$@"; fi
    }
''' + _activation_body()
    result = _run(tmp_path, body, replies=replies)
    assert (result.returncode == 0) is accepted, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert sum("executions list" in c for c in calls) == (1 if accepted else 10)
    assert any("logging read" in c and "real-reconciliation" in c for c in calls) is accepted
    if not accepted:
        assert (tmp_path / "sleeps").read_text().splitlines() == ["10"] * 9


@pytest.mark.parametrize("real_age", [100, 400, None])
def test_latest_succeeded_skip_scans_without_waiting(tmp_path: Path, real_age: int | None) -> None:
    replies = _replies()
    replies["execution"]["metadata"]["name"] = "single-flight-skip"
    replies["worker"]["status"]["latestCreatedExecution"]["name"] = "single-flight-skip"
    replies["executions"] = [replies["execution"]]
    if real_age is not None:
        real = copy.deepcopy(replies["execution"])
        real["metadata"]["name"] = "real-reconciliation"
        real["status"]["completionTime"] = (datetime.now(UTC) - timedelta(seconds=real_age)).isoformat()
        replies["executions"].append(real)
    body = '''sleep() { echo unexpected-sleep >&2; exit 99; }
    eval "$(declare -f gc | sed '1s/gc/recorded_gc/')"
    export -f recorded_gc
    gc() {
      if [ "$1 $2" = "logging read" ] && [[ "$*" == *single-flight-skip* ]]; then
        echo "$*" >> "$FIXTURES/calls"
        echo '[]'
      else recorded_gc "$@"; fi
    }
''' + _activation_body()
    result = _run(tmp_path, body, replies=replies)
    assert (result.returncode == 0) is (real_age == 100), result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert sum("executions list" in c for c in calls) == 1
    assert "unexpected-sleep" not in result.stderr


@pytest.mark.parametrize("latest_status", ["EXECUTION_FAILED", "EXECUTION_CANCELLED"])
@pytest.mark.parametrize("same_configuration", [False, True])
def test_latest_terminal_failure_is_classified_by_configuration(
    tmp_path: Path, latest_status: str, same_configuration: bool,
) -> None:
    replies = _replies()
    now = datetime.now(UTC)
    success = replies["execution"]
    success["metadata"]["name"] = "real-current-reconciliation"
    success["status"]["completionTime"] = (now - timedelta(seconds=100)).isoformat()
    failure = copy.deepcopy(success)
    failure["metadata"]["name"] = "latest-failure"
    failure["status"]["completionTime"] = (now - timedelta(seconds=20)).isoformat()
    failure["status"]["conditions"] = [{
        "type": "Completed", "status": "False",
        "reason": "Cancelled" if latest_status == "EXECUTION_CANCELLED" else "NonZeroExitCode",
    }]
    if not same_configuration:
        failure["spec"]["template"]["spec"]["containers"][0]["image"] = "old-image"
    replies["worker"]["status"]["latestCreatedExecution"] = {
        "name": "latest-failure", "completionStatus": latest_status,
    }
    replies["executions"] = [failure, success]
    result = _run(tmp_path, 'sleep() { echo unexpected-sleep >&2; exit 99; }\n' + _activation_body(), replies=replies)
    assert (result.returncode == 0) is not same_configuration, result.stderr
    calls = (tmp_path / "calls").read_text().splitlines()
    assert sum("executions list" in c for c in calls) == 1
    assert any("logging read" in c and "real-current-reconciliation" in c for c in calls) is not same_configuration
    assert "unexpected-sleep" not in result.stderr
