"""Execute the worker release step with the existing CLI harness."""
from __future__ import annotations

import copy
import json
import runpy
import shutil
from pathlib import Path

import pytest

from tests.deploy_script_harness import (
    SCRIPT_FIXTURES,
    DeployScriptHarness,
    ScriptFixture,
    summarise,
)
from tests.test_superseded_deploy import _finalize_outcome, _needs
from trusted_router.config import Settings

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = "scripts/deploy/settle_drain_worker.sh"
worker_plan = runpy.run_path(str(ROOT / "scripts/deploy/settle_drain_config.py"))["worker_plan"]


def router_revision():
    env = dict(TR_GCP_PROJECT_ID="quill-cloud-proxy", TR_SPANNER_INSTANCE_ID="test-instance",
               TR_SPANNER_DATABASE_ID="test-database", TR_RELEASE="a" * 7,
               TR_SOURCE_REVISION="a" * 40, TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED="false",
               TR_STRIPE_SECRET_KEY="must-not-copy", TR_INTERNAL_GATEWAY_TOKEN="must-not-copy")  # noqa: S106
    return {"spec": {"serviceAccountName": "router@quill-cloud-proxy.iam.gserviceaccount.com",
                     "containers": [{"image": "us-central1-docker.pkg.dev/test/router@sha256:" + "b" * 64,
                                     "env": [{"name": k, "value": v} for k, v in env.items()] +
                                     [{"name": "TR_SENTRY_DSN", "valueFrom": {"secretKeyRef": {"name": "trustedrouter-sentry-dsn", "key": "latest"}}}]}]}}


def test_worker_plan_validates_with_only_sentry_and_does_not_mutate_router():
    original = router_revision()
    before = copy.deepcopy(original)
    plan = worker_plan(original)
    cfg = Settings(sentry_dsn="https://public@example.com/1", **{name.removeprefix("TR_").lower(): value for name, value in plan["env"].items()})
    assert cfg.service_surface == "settle-drain" and cfg.settle_outbox_fast_drain_enabled
    assert cfg.settle_outbox_stall_seconds == 26
    assert plan["env"]["TR_LOOP_STALL_EXIT_SECONDS"] == "30"
    assert plan["secrets"] == {"TR_SENTRY_DSN": {"name": "trustedrouter-sentry-dsn", "key": "latest"}}
    assert cfg.settle_outbox_lease_seconds == 30
    assert cfg.settle_outbox_pass_budget_seconds == 2
    assert cfg.settle_outbox_poll_interval_seconds == cfg.settle_outbox_health_publish_interval_seconds == 1
    assert cfg.settle_outbox_claim_batch == cfg.settle_outbox_worker_concurrency == 4
    assert original == before
    assert not any("SECRET" in k or "TOKEN" in k or "PASSWORD" in k or "PRIVATE_KEY" in k for k in plan["env"])


@pytest.mark.parametrize("mutation", ["image", "fast", "revision", "missing", "duplicate"])
def test_worker_plan_rejects_ambiguous_or_unreleased_router(mutation):
    revision = router_revision()
    container = revision["spec"]["containers"][0]
    if mutation == "image":
        container["image"] = "router:latest"
    elif mutation == "missing":
        container["env"] = []
    elif mutation == "duplicate":
        container["env"].append(container["env"][0])
    else:
        name = "TR_SOURCE_REVISION" if mutation == "revision" else "TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED"
        next(e for e in container["env"] if e["name"] == name)["value"] = "true"
    with pytest.raises(ValueError):
        worker_plan(revision)


@pytest.mark.parametrize("hotfix", [None, "false", "true"])
@pytest.mark.parametrize("failure,network,subnet", [
    ("", None, None), ("", "worker-network", "worker-subnet"), ("", "", ""),
    *((failure, None, None) for failure in ("ci", "ci_pending", "bake", "mutex", "held", "public", "enabled_worker", "idle", "missing_mutex", "wrong_cloud", "held_region", "other_region")),
])
def test_deploy_executes_gates_and_worker_only_commands(tmp_path, monkeypatch, failure, network, subnet, hotfix):
    harness = DeployScriptHarness(tmp_path)
    for relative in [SCRIPT, "scripts/deploy/settle_drain_config.py"]:
        harness.write_script(relative, (ROOT / relative).read_text())
    # Existing coordinator/gate internals have their own execution tests. Here
    # test that this new caller invokes them and propagates refusal.
    harness.write_script("scripts/deploy/deploy_mutex.sh",
                         'deploy_mutex_assert() { gcloud worker-test-mutex; }\n')
    harness.write_script("scripts/deploy/cloud_bake_gate.sh",
                         'cloud_bake_gate() { gcloud worker-test-bake "$1"; }\n')
    router = router_revision()
    plan_config = (ROOT / "scripts/deploy/settle_drain_config.py").read_text()
    if failure == "idle":
        enabled = '"TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED": "true"'
        assert plan_config.count(enabled) == 1
        plan_config = plan_config.replace(enabled, '"TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED": "false"')
        harness.write_script("scripts/deploy/settle_drain_config.py", plan_config)
    namespace = {}
    exec(compile(plan_config, "settle_drain_config.py", "exec"), namespace)  # noqa: S102 - repository config
    plan = namespace["worker_plan"](router)
    actual = {"spec": {"serviceAccountName": plan["service_account"], "containers": [
        {"image": plan["image"], "env": [{"name": k, "value": v} for k, v in plan["env"].items()] +
         [{"name": k, "valueFrom": {"secretKeyRef": v}} for k, v in plan["secrets"].items()]}]}}
    def service(name):
        return json.dumps({"status": {"traffic": [{"percent": 100, "revisionName": name}]}})
    responses = (
        (r"run services describe trusted-router .*--format=json", service("trusted-router-active")),
        (r"run revisions describe trusted-router-active ", json.dumps(router)),
        (r"run services describe trusted-router-settle-drain ", service("drain-active")),
        (r"run revisions describe drain-active ", json.dumps(actual)),
        (r"run services list ", json.dumps([{"metadata": {"name": "trusted-router-settle-drain"}}]) if failure == "enabled_worker" else "[]"),
        (r"gh run list ", json.dumps([{"headSha": "a" * 40, "status": "in_progress" if failure == "ci_pending" else "completed",
                                      "conclusion": None if failure == "ci_pending" else ("failure" if failure == "ci" else "success")}])),
        (r"run services get-iam-policy ", json.dumps({"bindings": [{"members": ["allUsers"]}]}) if failure == "public" else "{}"),
    )
    failures = (f"worker-test-{failure}",) if failure in {"bake", "mutex"} else ()
    monkeypatch.setitem(SCRIPT_FIXTURES, SCRIPT, ScriptFixture(responses=responses, failures=failures))
    network_env = {} if network is None else {
        "TR_CLOUD_RUN_NETWORK": network, "TR_CLOUD_RUN_SUBNET": subnet,
    }
    run = harness.run(SCRIPT, extra_env={**network_env,
        **({"TR_SETTLE_DRAIN_HOTFIX": hotfix} if hotfix is not None else {}),
        "TR_DEPLOY_MUTEX_OPERATION": "" if failure == "missing_mutex" else "a" * 32,
        "TR_DEPLOY_MUTEX_CLOUD": "aws" if failure == "wrong_cloud" else "gcp",
        "TR_DEPLOY_HOLD_REGIONS": {"held": "all", "held_region": " us-east4, us-central1 ",
                                 "other_region": "us-east4"}.get(failure, "")})
    deploys = [c for c in run.calls if c[3:5] == ["run", "deploy"]]
    if failure in {"held", "held_region"}:
        assert run.returncode == 0, summarise(run)
        assert "held: settle-drain worker not deployed" in run.stdout
        assert not run.calls, summarise(run)
        return
    if failure in {"bake", "mutex", "public", "missing_mutex", "wrong_cloud"} or (
        failure in {"ci", "ci_pending"} and hotfix != "true"
    ):
        assert run.returncode != 0, summarise(run)
        assert len(deploys) == (1 if failure == "public" else 0)
        if failure in {"missing_mutex", "wrong_cloud"}:
            assert not run.calls, summarise(run)
        return
    assert run.returncode == 0, summarise(run)
    ci_calls = [c for c in run.calls if c[:3] == ["gh", "run", "list"]]
    warning = "::warning::HOTFIX MODE — skipping the CI-green gate."
    if hotfix == "true":
        assert not ci_calls
        assert warning in run.stdout
    else:
        assert len(ci_calls) == 1
        assert ci_calls[0][ci_calls[0].index("--commit") + 1] == "a" * 40
        assert warning not in run.stdout
    [call] = deploys
    assert call[5] == "trusted-router-settle-drain"
    assert plan["env"]["TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED"] == ("false" if failure == "idle" else "true")
    assert router == router_revision()
    assert "--no-traffic" not in call
    for flag, value in [("--region", "us-central1"), ("--min", "1"), ("--max", "1"),
        ("--min-instances", "1"), ("--max-instances", "1"), ("--ingress", "internal"),
        ("--image", plan["image"]), ("--service-account", plan["service_account"]),
        ("--network", network or "default"), ("--subnet", subnet or "default"),
        ("--vpc-egress", "private-ranges-only")]:
        assert call[call.index(flag) + 1] == value
    assert {"--no-cpu-throttling", "--no-allow-unauthenticated", "--set-secrets"} <= set(call)
    assert "--allow-unauthenticated" not in call and "--clear-secrets" not in call
    assert call[call.index("--set-secrets") + 1] == "TR_SENTRY_DSN=trustedrouter-sentry-dsn:latest"
    assert json.loads(json.dumps(plan["env"])) == dict(
        item.split("=", 1) for item in call[call.index("--set-env-vars") + 1].removeprefix("^|^").split("|"))
    assert any("worker-test-bake" in c for c in run.calls[:run.calls.index(call)])
    assert sum("worker-test-mutex" in c for c in run.calls[:run.calls.index(call)]) == 2
    assert not any("scheduler" in c or "update-traffic" in c for c in run.calls)


@pytest.mark.skipif(shutil.which("jq") is None or shutil.which("bash") is None, reason="the step needs bash and jq")
@pytest.mark.parametrize("hold", ["all", "us-central1"])
def test_held_worker_and_healthy_parent_release_reservation(tmp_path, hold):
    harness = DeployScriptHarness(tmp_path)
    harness.write_script(SCRIPT, (ROOT / SCRIPT).read_text())
    run = harness.run(SCRIPT, extra_env={"TR_DEPLOY_HOLD_REGIONS": hold})
    assert run.returncode == 0, summarise(run)
    assert not run.calls, summarise(run)
    assert "held: settle-drain worker not deployed" in run.stdout
    worker_result = "success" if run.returncode == 0 else "failure"
    assert _finalize_outcome(_needs(**{
        "settle-drain-worker": {"result": worker_result, "outputs": {}},
    })) == ("success", 0)
