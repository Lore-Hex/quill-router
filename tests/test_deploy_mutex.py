"""Execute the shell compatibility API against the real coordinator + fake GCS.

Legacy expiration/takeover tests are replaced by retained-failure recovery tests
in test_cloud_rollout_coordinator: expiry must no longer grant deployment rights.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]

GCLOUD = r'''import fcntl, json, os, sys
from pathlib import Path
a = sys.argv[1:]
p = Path(os.environ["FAKE_GCS"])
with p.with_suffix(".lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    with p.with_suffix(".calls").open("a") as log:
        log.write(json.dumps(a) + "\n")
    if os.environ.get("DENIED"):
        sys.stderr.write("403 denied"); sys.exit(1)
    if a[:3] == ["storage", "buckets", "describe"]:
        print(json.dumps({"name": "test", "lifecycle_config": {"rule": []}})); sys.exit(0)
    stored = json.loads(p.read_text()) if p.exists() else {"generation": 0, "data": None}
    if a[:3] == ["storage", "objects", "describe"]:
        if not stored["generation"]:
            sys.stderr.write("404 not found"); sys.exit(1)
        print(stored["generation"]); sys.exit(0)
    if a[:2] == ["storage", "cp"]:
        if a[2].startswith("gs://"):
            if int(a[2].rsplit("#", 1)[1]) != stored["generation"]:
                sys.stderr.write("412 conditionNotMet"); sys.exit(1)
            Path(a[3]).write_text(json.dumps(stored["data"])); sys.exit(0)
        expected = int(a[-1].split("=")[1])
        if expected != stored["generation"]:
            sys.stderr.write("412 conditionNotMet"); sys.exit(1)
        p.write_text(json.dumps({"generation": expected + 1, "data": json.loads(Path(a[2]).read_text())}))
        sys.exit(0)
    raise SystemExit("unexpected cloud operation: " + repr(a))
'''


@pytest.fixture
def mutex(tmp_path):
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    for name in ("cloud_rollout.py", "deploy_mutex.sh"):
        shutil.copy(ROOT / "scripts/deploy" / name, deploy / name)
    source = deploy / "cloud_rollout.py"
    source.write_text(source.read_text().replace(
        'if __name__ == "__main__":',
        'probe_cloud = lambda cloud: "a" * 40\nif __name__ == "__main__":',
    ))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python3").symlink_to(sys.executable)
    gcloud = bin_dir / "gcloud"
    gcloud.write_text(f"#!{sys.executable}\n{GCLOUD}")
    gcloud.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith("TR_DEPLOY_")}
    env.update(PATH=f"{bin_dir}:{env['PATH']}", FAKE_GCS=str(tmp_path / "state.json"))

    def run(command, cloud="gcp", fence=None, **extra):
        return subprocess.run(["bash", str(deploy / "deploy_mutex.sh"), command],  # noqa: S603,S607
                              env={**env, "TR_DEPLOY_MUTEX_CLOUD": cloud, **(fence or {}), **extra},
                              capture_output=True, text=True, timeout=20)
    return run, env, deploy


def exported(result):
    assert result.returncode == 0, result.stderr
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


def state(env):
    return json.loads(Path(env["FAKE_GCS"]).read_text())["data"]


def test_two_clouds_one_atomic_journal_third_refused(mutex):
    run, env, _ = mutex
    with ThreadPoolExecutor(3) as pool:
        results = list(pool.map(lambda cloud: run("acquire", cloud), ("gcp", "aws", "azure")))
    assert sum(r.returncode == 0 for r in results) == 2
    assert len(state(env)["leases"]) == 2


def test_same_cloud_components_are_exclusive(mutex):
    run, _, _ = mutex
    exported(run("acquire"))
    assert run("acquire", TR_DEPLOY_COMPONENT="gateway").returncode != 0


def test_one_owner_cannot_release_another(mutex):
    run, env, _ = mutex
    first = exported(run("acquire", "gcp"))
    second = exported(run("acquire", "aws"))
    assert run("release", "gcp", {**second, "TR_DEPLOY_MUTEX_CLOUD": "gcp"}).returncode != 0
    exported(run("release", "gcp", first))
    assert set(state(env)["leases"]) == {"aws"}
    assert state(env)["leases"]["aws"]["operation_id"] == second["TR_DEPLOY_MUTEX_OPERATION"]


def test_nested_scope_only_outer_releases_and_clears_fence(mutex):
    _, env, deploy = mutex
    script = '''source "$1"
deploy_mutex_acquire >/dev/null
first="$TR_DEPLOY_MUTEX_OPERATION"
deploy_mutex_acquire >/dev/null
deploy_mutex_release
test "$TR_DEPLOY_MUTEX_OPERATION" = "$first"
deploy_mutex_assert
deploy_mutex_release
test -z "${TR_DEPLOY_MUTEX_OPERATION:-}"
deploy_mutex_acquire >/dev/null
test "$TR_DEPLOY_MUTEX_OPERATION" != "$first"
deploy_mutex_finish 0
'''
    result = subprocess.run(["bash", "-euc", script, "test", str(deploy / "deploy_mutex.sh")],  # noqa: S603,S607
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert state(env)["leases"] == {}


def test_inherited_scope_does_not_release_workflow_owner(mutex):
    run, env, deploy = mutex
    fence = exported(run("acquire"))
    result = subprocess.run(["bash", "-euc",  # noqa: S603,S607
                             'source "$1"; deploy_mutex_acquire; deploy_mutex_finish 0',
                             "test", str(deploy / "deploy_mutex.sh")],
                            env={**env, **fence}, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert "gcp" in state(env)["leases"]


def test_failed_deploy_remains_blocked_and_permission_failure_never_unlocks(mutex):
    run, env, _ = mutex
    fence = exported(run("acquire"))
    assert run("release", fence=fence, DENIED="1").returncode != 0
    assert "gcp" in state(env)["leases"]
    exported(run("release", fence=fence, TR_DEPLOY_OUTCOME="failure"))
    assert state(env)["leases"]["gcp"]["state"] == "blocked"
    assert run("assert", fence=fence).returncode != 0
    assert run("release", fence=fence).returncode != 0


def test_journal_never_deleted_and_legacy_delete_generation_is_zero(mutex):
    run, env, _ = mutex
    fence = exported(run("acquire"))
    assert fence["TR_DEPLOY_MUTEX_GENERATION"] == "0"
    exported(run("release", fence=fence))
    assert state(env) == {"schema_version": 2, "leases": {}, "holdback": None}
    calls = Path(env["FAKE_GCS"]).with_suffix(".calls").read_text()
    assert '"rm"' not in calls


def test_global_workflow_reservation_spans_all_mutating_jobs_and_completion():
    workflow = yaml.safe_load((ROOT / ".github/workflows/deploy.yml").read_text())
    jobs = workflow["jobs"]
    for name in ("migrate-schema", "sync-runtime-secrets", "deploy", "public-surface-companion"):
        assert "admit-cloud" in jobs[name]["needs"]
    final = jobs["finalize-cloud"]
    assert "always()" in final["if"]
    assert {"deploy", "rollout-secondaries", "verify-cloud-complete", "public-surface-companion"} <= set(final["needs"])
    releases = [(name, step) for name, job in jobs.items() for step in job["steps"]
                if "deploy_mutex.sh release" in step.get("run", "")]
    assert [name for name, _ in releases] == ["finalize-cloud"]
    assert "TR_DEPLOY_OUTCOME=failure" in releases[0][1]["run"]
    acquire = jobs["admit-cloud"]["steps"][-1]
    assert 'fence="$(bash scripts/deploy/deploy_mutex.sh acquire)"' in acquire["run"]
    assert "| tee" not in acquire["run"]


@pytest.mark.parametrize("cloud", ["aws", "azure"])
def test_secondary_workflows_use_current_policy_with_selected_artifact(cloud):
    workflow = yaml.safe_load((ROOT / f".github/workflows/deploy-{cloud}-control-plane.yml").read_text())
    job = workflow["jobs"]["deploy"]
    assert job["env"]["TR_RELEASE_CHECKOUT"].endswith("/src")
    assert job["env"]["TR_DEPLOY_WAIT_SECONDS"] == "3600"
    assert any(step.get("with", {}).get("path") == "ops" for step in job["steps"])
    assert any("../ops/scripts/deploy/" in step.get("run", "") for step in job["steps"])


def test_bucket_does_not_auto_expire_failed_reservations():
    infra = (ROOT / "scripts/deploy/infra.sh").read_text()
    assert "tr-deploy-mutex-quill-cloud-proxy" in infra
    assert "'{\"rule\":[]}'" in infra
    assert "--public-access-prevention" in infra
    assert "--uniform-bucket-level-access" in infra


@pytest.mark.parametrize("name", ["rollout", "staged_traffic", "public_surface", "aws_ecs_control_plane", "azure_control_plane", "aws_eu_control_plane"])
def test_manual_mutators_bind_shared_guard_and_failed_outcome(name):
    script = (ROOT / f"scripts/deploy/{name}.sh").read_text()
    assert 'source "${SCRIPT_DIR}/deploy_mutex.sh"' in script
    assert "deploy_mutex_acquire" in script
    assert "deploy_mutex_finish" in script
