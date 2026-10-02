from __future__ import annotations

import copy
import json
import subprocess
import threading
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.deploy import cloud_rollout as rollout


class MemoryStore:
    def __init__(self):
        self.record = None
        self.generation = 0
        self.lock = threading.Lock()
        self.max_leases = 0

    def read(self):
        with self.lock:
            return copy.deepcopy(self.record), self.generation

    def write(self, record, generation):
        with self.lock:
            if generation != self.generation:
                raise rollout.Conflict()
            self.record = copy.deepcopy(record)
            self.generation += 1
            self.max_leases = max(self.max_leases, len(record["leases"]))


@pytest.fixture
def system():
    store = MemoryStore()
    health = {c: "a" * 40 for c in rollout.CLOUDS}
    clock = [1000]

    def probe(cloud):
        if health[cloud] is None:
            raise rollout.Refused("unhealthy")
        return health[cloud]

    coordinator = rollout.Coordinator(store, probe, lambda: clock[0])
    return SimpleNamespace(store=store, health=health, clock=clock, coordinator=coordinator)


def acquire(system, cloud, component="control-plane"):
    return system.coordinator.acquire(cloud, f"manual:{cloud}", component, 3600)


def test_two_distinct_clouds_allowed_and_third_forbidden(system):
    first = acquire(system, "gcp")
    second = acquire(system, "aws", "gateway")
    assert system.store.record["holdback"]["cloud"] == "azure"
    system.coordinator.check("gcp", first["operation_id"])
    system.coordinator.check("aws", second["operation_id"])
    with pytest.raises(rollout.Busy, match="third"):
        acquire(system, "azure")


@pytest.mark.parametrize("cloud", rollout.CLOUDS)
def test_same_cloud_gateway_and_control_plane_are_exclusive(system, cloud):
    acquire(system, cloud)
    with pytest.raises(rollout.Busy, match="already reserved"):
        acquire(system, cloud, "gateway")


def test_simultaneous_three_cloud_admission_never_exceeds_two(system):
    start = threading.Barrier(3)

    def request(cloud):
        start.wait(timeout=5)
        try:
            acquire(system, cloud)
            return True
        except rollout.Busy:
            return False

    with ThreadPoolExecutor(max_workers=3) as workers:
        outcomes = list(workers.map(request, rollout.CLOUDS))
    assert sum(outcomes) == 2
    assert system.store.max_leases == 2
    assert len(system.store.record["leases"]) == 2


def test_success_releases_only_owner_and_third_catches_up_without_24h(system):
    first = acquire(system, "gcp")
    second = acquire(system, "aws")
    system.coordinator.release("gcp", first["operation_id"], True)
    assert system.store.record["leases"]["aws"] == second
    acquire(system, "azure")
    assert set(system.store.record["leases"]) == {"aws", "azure"}
    assert system.store.record["holdback"]["cloud"] == "gcp"


def test_expiry_never_frees_a_mutating_cloud(system):
    first = acquire(system, "gcp")
    acquire(system, "aws")
    system.clock[0] += 10_000
    with pytest.raises(rollout.Busy):
        acquire(system, "azure")
    with pytest.raises(rollout.Refused, match="expired"):
        system.coordinator.check("gcp", first["operation_id"])
    assert len(system.store.record["leases"]) == 2


def test_failure_holds_slot_and_recovery_requires_stopped_writer_and_health(system):
    first = acquire(system, "gcp")
    acquire(system, "aws")
    op = first["operation_id"]
    system.coordinator.release("gcp", op, False)
    assert system.store.record["leases"]["gcp"]["state"] == "blocked"
    with pytest.raises(rollout.Busy):
        acquire(system, "azure")
    with pytest.raises(rollout.Refused, match="recovery"):
        system.coordinator.release("gcp", op, True)
    with pytest.raises(rollout.Refused, match="still be mutating"):
        system.coordinator.recover("gcp", op, lambda _: False)
    system.health["gcp"] = None
    with pytest.raises(rollout.Refused):
        system.coordinator.recover("gcp", op, lambda _: True)
    assert "gcp" in system.store.record["leases"]
    system.health["gcp"] = "b" * 40
    system.coordinator.recover("gcp", op, lambda _: True)
    assert set(system.store.record["leases"]) == {"aws"}


@pytest.mark.parametrize("command", ["check", "release", "recover"])
def test_stale_or_wrong_owner_cannot_touch_peer(system, command):
    first = acquire(system, "gcp")
    before = system.store.read()
    args = {"check": (), "release": (True,), "recover": (lambda _: True,)}[command]
    with pytest.raises(rollout.Refused, match="not owned"):
        getattr(system.coordinator, command)("aws", first["operation_id"], *args)
    assert system.store.read() == before


def test_unhealthy_remaining_cloud_blocks_second_admission(system):
    acquire(system, "gcp")
    system.health["azure"] = None
    before = system.store.read()
    with pytest.raises(rollout.Refused, match="no unchanged healthy"):
        acquire(system, "aws")
    assert system.store.read() == before


def test_protected_cloud_drift_or_outage_stops_forward_progress(system):
    first = acquire(system, "gcp")
    acquire(system, "aws")
    system.health["azure"] = "b" * 40
    with pytest.raises(rollout.Refused, match="release changed"):
        system.coordinator.check("gcp", first["operation_id"])
    system.health["azure"] = None
    with pytest.raises(rollout.Refused):
        system.coordinator.check("gcp", first["operation_id"])


def test_failed_final_health_does_not_release_slot(system):
    first = acquire(system, "gcp")
    system.health["gcp"] = None
    before = system.store.read()
    with pytest.raises(rollout.Refused):
        system.coordinator.release("gcp", first["operation_id"], True)
    assert system.store.read() == before


def test_last_release_leaves_versioned_empty_journal_to_block_legacy_clients(system):
    first = acquire(system, "gcp")
    system.coordinator.release("gcp", first["operation_id"], True)
    assert system.store.record == {"schema_version": 2, "leases": {}, "holdback": None}


@pytest.mark.parametrize("legacy", [
    {"owner": "legacy", "operation_id": "x", "expires_at": "2000-01-01T00:00:00Z"},
    {"owner": "legacy", "operation_id": "x", "expires_at": "2999-01-01T00:00:00Z"},
    {"schema_version": 99},
])
def test_legacy_or_unknown_record_requires_safe_migration(system, legacy):
    system.store.record = legacy
    with pytest.raises(rollout.Busy):
        acquire(system, "gcp")
    assert system.store.record == legacy


@pytest.mark.parametrize("bad", [
    {"schema_version": 2},
    {"schema_version": 2, "leases": {"other": {}}, "holdback": None},
    {"schema_version": 2, "leases": {"gcp": {}}, "holdback": None},
    {"schema_version": 2, "leases": {}, "holdback": {"cloud": "aws"}},
])
def test_malformed_journal_fails_closed(bad):
    with pytest.raises(rollout.Refused):
        rollout.state_record(bad)


def test_cas_contention_is_bounded(system, monkeypatch):
    def conflict(*_):
        raise rollout.Conflict()
    monkeypatch.setattr(system.store, "write", conflict)
    with pytest.raises(rollout.Refused, match="contention"):
        acquire(system, "gcp")
    assert system.store.record is None


def test_ambiguous_write_leaves_reservation_no_cleanup(system, monkeypatch):
    write = system.store.write

    def lost_response(record, generation):
        write(record, generation)
        raise rollout.Refused("ambiguous")

    monkeypatch.setattr(system.store, "write", lost_response)
    with pytest.raises(rollout.Refused):
        acquire(system, "gcp")
    assert "gcp" in system.store.record["leases"]


def test_output_never_exposes_journal_generation_as_legacy_delete_fence(system, capsys):
    rollout.emit(acquire(system, "gcp"))
    output = capsys.readouterr().out
    assert "TR_DEPLOY_MUTEX_GENERATION=0\n" in output
    assert "TR_DEPLOY_MUTEX_CLOUD=gcp\n" in output


@pytest.mark.parametrize("cloud", rollout.CLOUDS)
def test_health_checks_are_cloud_local_and_bracket_release(cloud, monkeypatch):
    plane, gateway = rollout.PLANES[cloud]
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("/health"):
            return {"status": "ok"}
        return {"release": "a" * 40, "api_base_url": f"{gateway}/v1"}

    monkeypatch.setattr(rollout, "fetch", fetch)
    assert rollout.probe_cloud(cloud) == "a" * 40
    assert calls == [f"{plane}/trust/control-plane.json", f"{plane}/health",
                     f"{gateway}/health", f"{plane}/trust/control-plane.json"]


@pytest.mark.parametrize("problem", ["unknown", "wrong-cloud", "unhealthy", "changing", "not-string"])
def test_cloud_evidence_is_fail_closed(problem, monkeypatch):
    count = 0

    def fetch(url):
        nonlocal count
        if url.endswith("/health"):
            return {"status": "down" if problem == "unhealthy" else "ok"}
        count += 1
        return {
            "release": ("unknown" if problem == "unknown" else
                        ("b" * 40 if problem == "changing" and count == 2 else "a" * 40)),
            "api_base_url": (None if problem == "not-string" else
                             "https://api-aws.trustedrouter.com/v1" if problem == "wrong-cloud"
                             else "https://api-azure.trustedrouter.com/v1"),
        }

    monkeypatch.setattr(rollout, "fetch", fetch)
    with pytest.raises(rollout.Refused):
        rollout.probe_cloud("azure")


def test_redirects_are_rejected():
    with pytest.raises(rollout.Refused, match="redirect"):
        rollout.NoRedirect().redirect_request(None, None, 302, "", {}, "https://trustedrouter.com")


@pytest.mark.parametrize(("code", "retry_after", "attempts", "delays"), [
    (429, None, 3, [1, 2]), (503, "3", 3, [3, 3]),
    (403, None, 1, []), (429, "60", 1, []), (503, "invalid", 1, []),
])
def test_health_retry_is_bounded_and_respects_server_wait(monkeypatch, code, retry_after, attempts, delays):
    calls, sleeps = [], []

    def open_request(request, timeout):
        calls.append(request)
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        raise urllib.error.HTTPError(request.full_url, code, "unavailable", headers, None)

    monkeypatch.setattr(rollout.urllib.request, "build_opener", lambda _: SimpleNamespace(open=open_request))
    monkeypatch.setattr(rollout.time, "sleep", sleeps.append)
    with pytest.raises(urllib.error.HTTPError):
        rollout.fetch("https://aws.trustedrouter.com/health")
    assert len(calls) == attempts
    assert sleeps == delays


def test_gcs_all_writes_are_cas_and_no_delete(monkeypatch):
    monkeypatch.setattr(rollout.shutil, "which", lambda _: "/bin/gcloud")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert args[:3] == ["/bin/gcloud", "storage", "cp"]
        assert json.loads(Path(args[3]).read_text())["schema_version"] == 2
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(rollout.subprocess, "run", run)
    rollout.GCSStore().write(rollout.state_record(None), 42)
    assert calls[0][-2:] == [rollout.URI, "--if-generation-match=42"]


@pytest.mark.parametrize("error,expected", [("403 denied", rollout.Refused), ("412 conditionNotMet", rollout.Conflict)])
def test_gcs_write_errors_never_unlock(monkeypatch, error, expected):
    monkeypatch.setattr(rollout.shutil, "which", lambda _: "/bin/gcloud")
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", error)

    monkeypatch.setattr(rollout.subprocess, "run", run)
    with pytest.raises(expected):
        rollout.GCSStore().write(rollout.state_record(None), 5)
    assert len(calls) == 1
    assert "rm" not in calls[0]


@pytest.mark.parametrize("error,missing", [("404 Not Found", True), ("403 denied", False)])
def test_gcs_read_distinguishes_absence_from_permission_failure(monkeypatch, error, missing):
    monkeypatch.setattr(rollout.shutil, "which", lambda _: "/bin/gcloud")
    monkeypatch.setattr(rollout.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 1, "", error))
    if missing:
        assert rollout.GCSStore().read() == (None, 0)
    else:
        with pytest.raises(rollout.Refused, match="unreadable"):
            rollout.GCSStore().read()


@pytest.mark.parametrize("payload,allowed", [
    ({"name": "bucket"}, True),
    ({"name": "bucket", "lifecycle_config": {"rule": []}}, True),
    ({"lifecycle_config": {"rule": [{"action": {"type": "Delete"}}]}}, False),
    ({"lifecycle": {"rule": [{"action": {"type": "Delete"}}]}}, False),
    ({"lifecycle_config": "unknown"}, False),
    ({}, False),
])
def test_automatic_journal_deletion_is_refused(monkeypatch, payload, allowed):
    monkeypatch.setattr(rollout.shutil, "which", lambda _: "/bin/gcloud")
    monkeypatch.setattr(rollout.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 0, json.dumps(payload), ""))
    if allowed:
        rollout.GCSStore().verify_retention()
    else:
        with pytest.raises(rollout.Refused):
            rollout.GCSStore().verify_retention()


def test_recovery_refuses_running_workflow(monkeypatch):
    monkeypatch.setattr(rollout.shutil, "which", lambda _: "/bin/gh")
    monkeypatch.setattr(rollout.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 0, '{"status":"in_progress"}', ""))
    assert not rollout.owner_stopped({"owner": "https://github.com/Lore-Hex/quill-router/actions/runs/123"})


def test_recovery_accepts_completed_workflow(monkeypatch):
    monkeypatch.setattr(rollout.shutil, "which", lambda _: "/bin/gh")
    monkeypatch.setattr(rollout.subprocess, "run", lambda args, **kwargs:
                        subprocess.CompletedProcess(args, 0, '{"status":"completed"}', ""))
    assert rollout.owner_stopped({"owner": "https://github.com/Lore-Hex/quill-cloud-proxy/actions/runs/123"})


def test_short_lived_acquire_is_not_assumed_to_be_manual_owner(system, monkeypatch):
    monkeypatch.delenv("TR_DEPLOY_OWNER_PID", raising=False)
    lease = acquire(system, "gcp")
    assert lease["pid"] == 0
    assert not rollout.owner_stopped(lease)
