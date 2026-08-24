from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MUTEX_SCRIPT = ROOT / "scripts" / "deploy" / "deploy_mutex.sh"

_GCLOUD_STUB = r'''#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import shutil
import sys

args = sys.argv[1:]
state = Path(os.environ["MUTEX_STUB_STATE"])
guard_path = state / "guard"
object_path = state / "object.json"
generation_path = state / "generation"
counter_path = state / "counter"
calls_path = Path(os.environ["MUTEX_STUB_CALLS"])
mutations_path = Path(os.environ["MUTEX_STUB_MUTATIONS"])


def append(path: Path, value: object) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, separators=(",", ":")) + "\n")


def precondition(name: str) -> str | None:
    prefix = f"--{name}="
    for index, argument in enumerate(args):
        if argument.startswith(prefix):
            return argument[len(prefix) :]
        if argument == f"--{name}" and index + 1 < len(args):
            return args[index + 1]
    return None


with guard_path.open("a+", encoding="utf-8") as guard:
    fcntl.flock(guard, fcntl.LOCK_EX)
    append(calls_path, args)

if args[:2] == ["storage", "cp"]:
    source = args[2]
    destination = args[3]
    if destination.startswith("gs://"):
        if precondition("if-generation-match") != "0":
            raise SystemExit(2)
        with guard_path.open("a+", encoding="utf-8") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            if object_path.exists():
                print("precondition failed", file=sys.stderr)
                raise SystemExit(1)
            generation = int(counter_path.read_text() or "0") + 1
            counter_path.write_text(str(generation), encoding="utf-8")
            shutil.copyfile(source, object_path)
            generation_path.write_text(str(generation), encoding="utf-8")
            record = json.loads(object_path.read_text(encoding="utf-8"))
            append(
                mutations_path,
                {
                    "action": "create",
                    "generation": generation,
                    "operation_id": record["operation_id"],
                },
            )
        raise SystemExit(0)

    requested_generation = None
    if "#" in source:
        source, requested_generation = source.rsplit("#", 1)
    if os.environ.get("MUTEX_STUB_DOWNLOAD_FAIL") == "1":
        print("injected download failure", file=sys.stderr)
        raise SystemExit(1)
    with guard_path.open("a+", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_SH)
        if not object_path.exists():
            print("not found: 404", file=sys.stderr)
            raise SystemExit(1)
        generation = generation_path.read_text(encoding="utf-8")
        if requested_generation is not None and requested_generation != generation:
            print("generation changed", file=sys.stderr)
            raise SystemExit(1)
        shutil.copyfile(object_path, destination)
    raise SystemExit(0)

if args[:3] == ["storage", "objects", "describe"]:
    replace_flag = state / "replace-on-describe"
    if replace_flag.exists():
        # Simulates the break-glass window: between our create and our
        # verification describe, an operator removed the lock and a second
        # acquirer immediately created its own. The current object at the
        # name is theirs, at a NEW generation.
        with guard_path.open("a+", encoding="utf-8") as guard:
            fcntl.flock(guard, fcntl.LOCK_EX)
            if object_path.exists() and replace_flag.exists():
                replace_flag.unlink()
                generation = int(counter_path.read_text() or "0") + 1
                counter_path.write_text(str(generation), encoding="utf-8")
                record = json.loads(object_path.read_text(encoding="utf-8"))
                record["operation_id"] = "someone-elses-operation"
                object_path.write_text(
                    json.dumps(record, separators=(",", ":")), encoding="utf-8"
                )
                generation_path.write_text(str(generation), encoding="utf-8")
                append(
                    mutations_path,
                    {"action": "replace", "generation": generation},
                )
    with guard_path.open("a+", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_SH)
        if not object_path.exists():
            print("not found: 404", file=sys.stderr)
            raise SystemExit(1)
        print(generation_path.read_text(encoding="utf-8"))
    raise SystemExit(0)

if args[:2] == ["storage", "rm"]:
    if os.environ.get("MUTEX_STUB_RM_FAIL") == "1":
        print("injected delete failure", file=sys.stderr)
        raise SystemExit(1)
    expected_generation = precondition("if-generation-match")
    with guard_path.open("a+", encoding="utf-8") as guard:
        fcntl.flock(guard, fcntl.LOCK_EX)
        if not object_path.exists():
            print("not found: 404", file=sys.stderr)
            raise SystemExit(1)
        generation = generation_path.read_text(encoding="utf-8")
        # Real gcloud deletes unconditionally when no precondition is given.
        # Modeling an unfenced rm as a failure here would let a test of an
        # unfenced-delete bug pass while production deleted a live lock.
        if expected_generation is not None and expected_generation != generation:
            print("precondition failed", file=sys.stderr)
            raise SystemExit(1)
        object_path.unlink()
        generation_path.unlink()
        append(
            mutations_path,
            {"action": "delete", "generation": int(generation)},
        )
    raise SystemExit(0)

print(f"unsupported gcloud invocation: {args!r}", file=sys.stderr)
raise SystemExit(2)
'''


@dataclass
class MutexHarness:
    root: Path
    bin_dir: Path
    state_dir: Path
    calls_path: Path
    mutations_path: Path

    @classmethod
    def build(cls, root: Path) -> MutexHarness:
        bin_dir = root / "bin"
        state_dir = root / "state"
        temp_dir = root / "tmp"
        home_dir = root / "home"
        for directory in (bin_dir, state_dir, temp_dir, home_dir):
            directory.mkdir(parents=True)
        calls_path = root / "calls.jsonl"
        mutations_path = root / "mutations.jsonl"
        calls_path.write_text("", encoding="utf-8")
        mutations_path.write_text("", encoding="utf-8")
        (state_dir / "counter").write_text("0", encoding="utf-8")
        gcloud = bin_dir / "gcloud"
        gcloud.write_text(_GCLOUD_STUB, encoding="utf-8")
        gcloud.chmod(0o755)
        (bin_dir / "python3").symlink_to(sys.executable)
        return cls(root, bin_dir, state_dir, calls_path, mutations_path)

    def env(self, owner: str, **extra: str) -> dict[str, str]:
        env = {
            "PATH": f"{self.bin_dir}:/bin:/usr/bin",
            "HOME": str(self.root / "home"),
            "TMPDIR": str(self.root / "tmp"),
            "LANG": "C",
            "MUTEX_STUB_STATE": str(self.state_dir),
            "MUTEX_STUB_CALLS": str(self.calls_path),
            "MUTEX_STUB_MUTATIONS": str(self.mutations_path),
            "TR_DEPLOY_MUTEX_BUCKET": "trusted-router-mutex-test",
            "TR_DEPLOY_MUTEX_OWNER": owner,
            "TR_DEPLOY_MUTEX_TOOL": "manual",
            "TR_DEPLOY_MUTEX_TTL_SECONDS": "60",
            **extra,
        }
        assert "TR_SENTRY_DSN" not in env
        return env

    def run(
        self,
        command: str,
        owner: str,
        **extra: str,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - repo script and isolated stub PATH.
            ["bash", str(MUTEX_SCRIPT), command],  # noqa: S607
            cwd=ROOT,
            env=self.env(owner, **extra),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )

    def popen(self, owner: str) -> subprocess.Popen[str]:
        return subprocess.Popen(  # noqa: S603 - repo script and isolated stub PATH.
            ["bash", str(MUTEX_SCRIPT), "acquire"],  # noqa: S607
            cwd=ROOT,
            env=self.env(owner),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

    def record(self) -> dict[str, object]:
        return json.loads((self.state_dir / "object.json").read_text(encoding="utf-8"))

    def generation(self) -> int:
        return int((self.state_dir / "generation").read_text(encoding="utf-8"))

    def calls(self) -> list[list[str]]:
        return [
            json.loads(line)
            for line in self.calls_path.read_text(encoding="utf-8").splitlines()
        ]

    def mutations(self) -> list[dict[str, object]]:
        return [
            json.loads(line)
            for line in self.mutations_path.read_text(encoding="utf-8").splitlines()
        ]

    def clear_calls(self) -> None:
        self.calls_path.write_text("", encoding="utf-8")


@pytest.fixture
def mutex(tmp_path: Path) -> MutexHarness:
    return MutexHarness.build(tmp_path)


def _exported(stdout: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)


def test_two_concurrent_acquires_have_exactly_one_winner(
    mutex: MutexHarness,
) -> None:
    contenders = [mutex.popen("manual:one@test"), mutex.popen("manual:two@test")]
    results = [process.communicate(timeout=20) for process in contenders]
    returncodes = [process.returncode for process in contenders]

    assert sorted(returncodes) == [0, 1]
    winner_index = returncodes.index(0)
    loser_index = returncodes.index(1)
    winner_exports = _exported(results[winner_index][0])
    assert mutex.record()["operation_id"] == winner_exports[
        "TR_DEPLOY_MUTEX_OPERATION"
    ]
    assert "deploy_mutex.blocked" in results[loser_index][1]
    assert mutex.mutations() == [
        {
            "action": "create",
            "generation": 1,
            "operation_id": winner_exports["TR_DEPLOY_MUTEX_OPERATION"],
        }
    ]


def test_expired_holder_is_replaced_and_old_release_is_fenced(
    mutex: MutexHarness,
) -> None:
    first = mutex.run("acquire", "manual:crashed@test")
    assert first.returncode == 0, first.stderr
    first_exports = _exported(first.stdout)
    first_generation = int(first_exports["TR_DEPLOY_MUTEX_GENERATION"])
    expired = mutex.record()
    expired["expires_at"] = "2000-01-01T00:00:00Z"
    (mutex.state_dir / "object.json").write_text(
        json.dumps(expired), encoding="utf-8"
    )

    replacement = mutex.run("acquire", "manual:replacement@test")
    assert replacement.returncode == 0, replacement.stderr
    replacement_exports = _exported(replacement.stdout)
    replacement_generation = int(
        replacement_exports["TR_DEPLOY_MUTEX_GENERATION"]
    )
    assert replacement_generation > first_generation
    assert "deploy_mutex.expired_takeover" in replacement.stderr
    assert "previous_owner=manual:crashed@test" in replacement.stderr

    stale_release = mutex.run(
        "release",
        "manual:crashed@test",
        TR_DEPLOY_MUTEX_OPERATION=first_exports["TR_DEPLOY_MUTEX_OPERATION"],
        TR_DEPLOY_MUTEX_GENERATION=str(first_generation),
    )
    assert stale_release.returncode == 0
    assert "deploy_mutex.release_failed" in stale_release.stderr
    assert mutex.generation() == replacement_generation
    assert mutex.record()["operation_id"] == replacement_exports[
        "TR_DEPLOY_MUTEX_OPERATION"
    ]


def test_stale_generation_cannot_release_current_owner(mutex: MutexHarness) -> None:
    acquired = mutex.run("acquire", "manual:owner@test")
    assert acquired.returncode == 0, acquired.stderr
    exported = _exported(acquired.stdout)
    generation = int(exported["TR_DEPLOY_MUTEX_GENERATION"])

    released = mutex.run(
        "release",
        "manual:owner@test",
        TR_DEPLOY_MUTEX_OPERATION=exported["TR_DEPLOY_MUTEX_OPERATION"],
        TR_DEPLOY_MUTEX_GENERATION=str(generation + 1),
    )

    assert released.returncode == 0
    assert "deploy_mutex.release_failed" in released.stderr
    assert mutex.generation() == generation


def test_reentrant_acquire_never_touches_storage(mutex: MutexHarness) -> None:
    env = mutex.env(
        "manual:nested@test",
        TR_DEPLOY_MUTEX_OPERATION="existing-operation",
        TR_DEPLOY_MUTEX_GENERATION="77",
    )
    result = subprocess.run(  # noqa: S603 - fixed shell and repo script.
        [
            "/bin/bash",
            "-c",
            'source "$1"; deploy_mutex_acquire >/dev/null; deploy_mutex_release',
            "mutex-reentrant-test",
            str(MUTEX_SCRIPT),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )

    assert result.returncode == 0
    assert "deploy_mutex.reentrant" in result.stderr
    assert "reason=scope_did_not_acquire" in result.stderr
    assert mutex.calls() == []
    assert mutex.mutations() == []


def test_release_failure_is_logged_but_does_not_change_exit_status(
    mutex: MutexHarness,
) -> None:
    acquired = mutex.run("acquire", "manual:owner@test")
    assert acquired.returncode == 0, acquired.stderr
    exported = _exported(acquired.stdout)
    mutex.clear_calls()

    released = mutex.run(
        "release",
        "manual:owner@test",
        TR_DEPLOY_MUTEX_OPERATION=exported["TR_DEPLOY_MUTEX_OPERATION"],
        TR_DEPLOY_MUTEX_GENERATION=exported["TR_DEPLOY_MUTEX_GENERATION"],
        MUTEX_STUB_RM_FAIL="1",
    )

    assert released.returncode == 0
    assert "deploy_mutex.release_failed" in released.stderr
    assert mutex.record()["operation_id"] == exported["TR_DEPLOY_MUTEX_OPERATION"]


def test_status_prints_generation_and_reports_unlocked(mutex: MutexHarness) -> None:
    acquired = mutex.run("acquire", "manual:owner@test")
    assert acquired.returncode == 0, acquired.stderr
    exported = _exported(acquired.stdout)

    status = mutex.run("status", "manual:inspector@test")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["generation"] == int(
        exported["TR_DEPLOY_MUTEX_GENERATION"]
    )

    released = mutex.run(
        "release",
        "manual:owner@test",
        TR_DEPLOY_MUTEX_OPERATION=exported["TR_DEPLOY_MUTEX_OPERATION"],
        TR_DEPLOY_MUTEX_GENERATION=exported["TR_DEPLOY_MUTEX_GENERATION"],
    )
    assert released.returncode == 0
    status = mutex.run("status", "manual:inspector@test")
    assert status.returncode == 0
    assert status.stdout == "unlocked\n"


def test_workflow_and_manual_scripts_share_the_mutex_scope() -> None:
    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    acquire = workflow.index("- name: Acquire production deployment mutex")
    rollout = workflow.index("- name: Deploy us-central1 (no-traffic)")
    release = workflow.index("- name: Release production deployment mutex")

    assert acquire < rollout < release
    assert 'deploy_mutex.sh acquire >> "$GITHUB_ENV"' in workflow[acquire:rollout]
    assert "${{ github.server_url }}/${{ github.repository }}/actions/runs/" in workflow[
        acquire:rollout
    ]
    assert "if: always()" in workflow[release : release + 180]
    assert "deploy_mutex.sh release" in workflow[release : release + 180]

    # These large scripts need unrelated Cloud Run/watchdog fixtures to finish;
    # the generation-aware behavioral tests above execute their shared helper,
    # while this assertion binds both manual entry points to its fail-closed guard.
    for relative in ("scripts/deploy/rollout.sh", "scripts/deploy/staged_traffic.sh"):
        script = (ROOT / relative).read_text(encoding="utf-8")
        assert 'source "${SCRIPT_DIR}/deploy_mutex.sh"' in script
        assert 'if [ -z "${TR_DEPLOY_MUTEX_OPERATION:-}" ]; then' in script
        assert "deploy_mutex_acquire" in script
        assert "deploy_mutex_release" in script


def test_infra_provisions_a_private_short_lived_mutex_bucket() -> None:
    infra = (ROOT / "scripts" / "deploy" / "infra.sh").read_text(encoding="utf-8")

    assert "tr-deploy-mutex-quill-cloud-proxy" in infra
    assert "--uniform-bucket-level-access" in infra
    assert "--public-access-prevention" in infra
    assert '{"rule":[{"action":{"type":"Delete"},"condition":{"age":1}}]}' in infra
    assert '--member="serviceAccount:${DEPLOY_SERVICE_ACCOUNT}"' in infra
    assert '--role="roles/storage.objectAdmin"' in infra


def test_unreadable_fence_leaves_the_lock_in_place(mutex: MutexHarness) -> None:
    """Never delete on ambiguity — review finding M2.

    A transient failure reading our own fence back must NOT remove the
    object: in the break-glass window the current object can belong to a
    different acquirer, and deleting it would let two deploys mutate
    production concurrently. The cost of leaving it is a TTL-bounded
    freeze, which is the safe side.
    """
    acquire = mutex.run("acquire", "owner-a", MUTEX_STUB_DOWNLOAD_FAIL="1")
    assert acquire.returncode == 1
    assert "reason=fence_unreadable" in acquire.stderr
    assert "cleanup=none" in acquire.stderr
    # The object survives, and no delete of any kind was attempted.
    assert (mutex.state_dir / "object.json").exists()
    assert all(m["action"] != "delete" for m in mutex.mutations())
    assert not any(c[:2] == ["storage", "rm"] for c in mutex.calls())


def test_replaced_lock_is_not_deleted_by_the_loser(mutex: MutexHarness) -> None:
    """Break-glass rm + immediate re-acquire between our create and verify.

    The verification must recognise the current object as someone else's and
    walk away. Deleting it — fenced or not — would kill the new holder's
    live lock.
    """
    (mutex.state_dir / "replace-on-describe").touch()
    acquire = mutex.run("acquire", "owner-a")
    assert acquire.returncode == 1
    assert "reason=lock_replaced" in acquire.stderr
    assert "holder_operation_id=someone-elses-operation" in acquire.stderr
    record = mutex.record()
    assert record["operation_id"] == "someone-elses-operation"
    assert all(m["action"] != "delete" for m in mutex.mutations())


def test_release_unsets_the_fence_so_reacquire_is_real(mutex: MutexHarness) -> None:
    """Review finding M1: stale exported fences must not survive release.

    acquire -> release -> acquire in ONE shell must perform a second real
    GCS acquisition, not a reentrant no-op against a lock that no longer
    exists.
    """
    script = f"""
set -euo pipefail
source {json.dumps(str(MUTEX_SCRIPT))}
deploy_mutex_acquire >/dev/null
deploy_mutex_release
if [ -n "${{TR_DEPLOY_MUTEX_OPERATION:-}}" ]; then
  echo "FENCE SURVIVED RELEASE" >&2
  exit 90
fi
deploy_mutex_acquire >/dev/null
"""
    result = subprocess.run(  # noqa: S603 - repo script and isolated stub PATH.
        ["bash", "-c", script],  # noqa: S607
        cwd=ROOT,
        env=mutex.env("owner-a"),
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    creates = [m for m in mutex.mutations() if m["action"] == "create"]
    deletes = [m for m in mutex.mutations() if m["action"] == "delete"]
    assert len(creates) == 2, "second acquire must be a real acquisition"
    assert len(deletes) == 1
    assert "deploy_mutex.reentrant" not in result.stderr


def test_deploy_job_timeout_stays_under_the_mutex_ttl() -> None:
    """Review finding M3: a deploy job outliving the lease invites a legal
    expired-lock takeover while the run is still shifting traffic."""
    import re

    workflow = (ROOT / ".github" / "workflows" / "deploy.yml").read_text(
        encoding="utf-8"
    )
    deploy_job = workflow.split("\n  deploy:\n", 1)[1].split("\n  public-surface", 1)[0]
    match = re.search(r"timeout-minutes:\s*(\d+)", deploy_job)
    assert match is not None, "deploy job must bound its runtime"
    ttl_match = re.search(
        r"TR_DEPLOY_MUTEX_TTL_SECONDS:-(\d+)",
        (ROOT / "scripts" / "deploy" / "deploy_mutex.sh").read_text(encoding="utf-8"),
    )
    assert ttl_match is not None
    assert int(match.group(1)) * 60 < int(ttl_match.group(1))
