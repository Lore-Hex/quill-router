"""P3 native serialization and slot races, selected by CI's emulator command."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import NOW_US, maximum_rows
from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore

pytestmark = pytest.mark.xdist_group("conformance-spanner-emulator")


@pytest.fixture(scope="module")
def shadow_emulator_resources():
    # Reuse the existing emulator/schema fixture in an isolated instance so the
    # empty snapshot witness cannot inherit outbox rows from adjacent suites.
    from tests.conformance.spanner_emulator import emulator_resources
    if "SPANNER_EMULATOR_HOST" in os.environ:
        assert os.environ.get("TR_CONFORMANCE_EMULATOR_SCHEMA") == "1", "configured emulator requires schema coverage"
    try:
        with emulator_resources() as resources:
            yield resources
    except pytest.skip.Exception as error:
        if "SPANNER_EMULATOR_HOST" in os.environ:
            pytest.fail(f"configured native case skipped: {error}")
        raise


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_six_rows(shadow_emulator_resources: Any, backend: str, monkeypatch: Any) -> None:
    database, _ = shadow_emulator_resources
    store = ObservationStore(database, wall=lambda: NOW_US/1e6)
    pages, counter, closure = maximum_rows(uuid4().hex*2)
    from types import SimpleNamespace

    from google.cloud.spanner_v1.transaction import Transaction
    from google.cloud.spanner_v1.types import CommitRequest
    captured = []
    commit = store.database.spanner_api.commit
    def encode(*args, **kwargs):
        request = CommitRequest(kwargs["request"])
        captured.append(request)
        return commit(*args, **kwargs)
    monkeypatch.setattr(store.database.spanner_api, "commit", encode)
    store.flush(pages, counter, closure, Budget(time.monotonic()+4))
    assert len(captured) == 1
    native = captured[0]
    fake = Transaction(SimpleNamespace())
    store.write(fake, [*pages, counter, closure], Budget(time.monotonic()+4))
    assert list(native.mutations) == list(fake._mutations)
    assert sum(len(row.body) for row in [*pages, counter, closure]) == 331776
    assert max(len(row.key) for row in pages) == 96
    assert 331776 < len(CommitRequest.serialize(native)) < 336000
    assert store.reconcile([*pages, counter, closure], Budget(time.monotonic()+4))


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_slot_race(shadow_emulator_resources: Any, backend: str) -> None:
    database, _ = shadow_emulator_resources
    store = ObservationStore(database)
    other = ObservationStore(database)
    # Warm the independent lanes before aligning the race. The cold-session
    # deadline path is covered by the native fault/checkout harness.
    store.read_shadow_health()
    other.read_shadow_health()
    time.sleep(2-time.time()%2+.005)
    now_us = int(time.time()*1e6)
    slot = now_us//2_000_000
    def contender(boot: str, contender_store: ObservationStore) -> dict[str, Any] | None:
        budget = Budget(time.monotonic()+.5)
        try:
            if not contender_store.claim(boot, slot, now_us, budget):
                return None
            value = contender_store.observe(boot, "us-central1", "a"*40, slot, budget)
            assert contender_store.publish(value, budget)
            return value
        except Exception:
            return None
    # The winner observes immediately, without waiting for the loser's timeout.
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(contender, "a", store)
        b = pool.submit(contender, "b", other)
        winners = [value for value in (a.result(), b.result()) if value is not None]
    assert len(winners) == 1
    value = winners[0]
    read = store.read_shadow_health()
    assert read is not None and read["body"] == value
    assert value["observed_at_ns"] <= read["committed_at_ns"] <= read["read_at_ns"]
    with pytest.raises(ValueError, match="a1-server-slot"):
        store.claim("future", slot+100, now_us+200_000_000, Budget(time.monotonic()+.5))
    assert not store.publish(value, Budget(time.monotonic()+.5))  # Same slot cannot overwrite.


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_checkpoint_fences(shadow_emulator_resources: Any, backend: str) -> None:
    from trusted_router.config import Settings
    from trusted_router.services.async_settle_shadow_health import Service
    database, _ = shadow_emulator_resources
    svc = Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), ObservationStore(database))
    day = next(iter(svc.journal.days))
    svc.journal.checkpoint(day)
    older = svc.counter_row(day, int(time.time()*1e6), closed=False)
    svc.journal.checkpoint(day)
    newer = svc.counter_row(day, int(time.time()*1e6), closed=False)
    svc.store.flush([], newer, None, Budget(time.monotonic()+4))
    with pytest.raises(ValueError, match="checkpoint_regression"):
        svc.store.flush([], older, None, Budget(time.monotonic()+4))
    assert svc.store.reconcile([newer], Budget(time.monotonic()+4))


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_reference_readback(shadow_emulator_resources: Any, backend: str) -> None:
    from tests.test_async_settle_shadow_observation import event_for
    from trusted_router.config import Settings
    from trusted_router.services.async_settle_shadow_health import Service
    database, _ = shadow_emulator_resources
    svc = Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), ObservationStore(database))
    svc.maintain()
    r = svc.port.reserve(int(time.time()*1e6))
    assert r is not None
    assert svc.port.finalize(r, event_for(r))
    assert svc.port.complete(r)
    svc.flush(time.monotonic()+4)
    assert svc.journal.days[r.day].hwm == 1


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_empty_nonempty_metadata(shadow_emulator_resources: Any, backend: str) -> None:
    import datetime as dt

    from google.cloud.spanner_v1 import KeySet
    database, _ = shadow_emulator_resources
    store = ObservationStore(database)
    slot = int(time.time())//2
    empty = store.observe("reader", "us-central1", "a"*40, slot, Budget(time.monotonic()+.5))
    assert empty["observed_at_ns"] > 0 and empty["backlog_count"] == 0
    identity = uuid4().hex
    with database.batch() as batch:
        batch.insert(table="tr_settle_outbox", columns=("authorization_id", "intent_kind", "settle_origin", "actual_cost_micro", "status", "created_at"),
            values=[(identity, "settle", "typed", 1, "dead", dt.datetime.now(dt.UTC))])
    try:
        nonempty = store.observe("reader", "us-central1", "a"*40, slot, Budget(time.monotonic()+.5))
        assert nonempty["observed_at_ns"] > empty["observed_at_ns"]
        assert nonempty["backlog_count"] == empty["backlog_count"]+1
        assert nonempty["dead_count"] == empty["dead_count"]+1
    finally:
        with database.batch() as batch:
            batch.delete("tr_settle_outbox", KeySet(keys=[(identity, "settle")]))


def native_service(database):
    from trusted_router.config import Settings
    from trusted_router.services.async_settle_shadow_health import Service
    return Service(Settings(environment="test", async_settle_shadow_policy="shadow-v2",
        async_settle_shadow_workspaces="ws", source_revision="a"*40), ObservationStore(database, wall=lambda: NOW_US/1e6))


@pytest.mark.parametrize("backend", ["spanner-emulator"])
@pytest.mark.parametrize("fault", ["lost", "late"])
def test_shadow_native_original_final_reply(shadow_emulator_resources, backend, fault):
    from tests.shadow_observation_faults import verify_final_fault
    database, _ = shadow_emulator_resources
    verify_final_fault(native_service(database), fault)


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_native_one_attempt_handoff(shadow_emulator_resources, backend):
    from tests.shadow_observation_faults import verify_handoff_fault
    database, _ = shadow_emulator_resources
    verify_handoff_fault(native_service(database))


@pytest.mark.parametrize("backend", ["spanner-emulator"])
@pytest.mark.parametrize("signum", ["SIGTERM", "SIGKILL"])
@pytest.mark.parametrize("point", ["before_first_flush", "during_commit", "before_ack", "after_ack", "before_result", "after_result"])
def test_shadow_native_process_restart(shadow_emulator_resources, backend, point, signum, tmp_path):
    import json
    import signal
    import subprocess
    import sys
    from pathlib import Path
    database, _ = shadow_emulator_resources
    boot = uuid4().hex
    args = [sys.executable, str(Path(__file__).parents[1]/"shadow_observation_restart.py"),
        database._instance._client.project, database._instance.instance_id, database.database_id, boot]
    with (tmp_path/"child.log").open("w") as errors:
        child = subprocess.Popen([*args, point], stdout=subprocess.PIPE, stderr=errors, text=True)  # noqa: S603
        try:
            assert child.stdout.readline().strip() == "BOUNDARY", (tmp_path/"child.log").read_text()
            child.send_signal(getattr(signal, signum))
            assert child.wait(timeout=180) == -getattr(signal, signum)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
    recovered = subprocess.run([*args, "recover"], capture_output=True, text=True, timeout=180)  # noqa: S603
    assert recovered.returncode == 0, recovered.stderr
    state = json.loads(recovered.stdout.splitlines()[-1])
    assert state["durable"] == (point == "after_result")
    assert state["missing_close"] == (point != "after_result")
    assert state["closure"] == (point in {"before_ack", "after_ack", "before_result", "after_result"})


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_native_exact_miss_then_late_landing(shadow_emulator_resources, backend, monkeypatch):
    from google.api_core.exceptions import DeadlineExceeded
    from google.cloud.spanner_v1.types import CommitRequest

    from trusted_router.async_settle_shadow_journal import CLOSE_RESULT_KIND, CLOSURE_KIND
    from trusted_router.storage_gcp_shadow_io import rpc_scope
    database, _ = shadow_emulator_resources
    svc = native_service(database)
    now = [100.]
    svc.store.clock = lambda: now[0]
    svc.closing_reason, svc.termination_deadline = "shutdown", 109.
    api = svc.store.database.spanner_api
    commit = api.commit
    pending = []
    def defer(*args, **kwargs):
        request = CommitRequest(kwargs["request"])
        kinds = [row[0] for mutation in request.mutations for row in mutation.insert_or_update.values]
        if CLOSURE_KIND in kinds:
            pending.append((args, kwargs))
            raise DeadlineExceeded("request withheld before native commit")
        return commit(*args, **kwargs)
    monkeypatch.setattr(api, "commit", defer)
    svc.flush(104.)
    assert len(pending) == 1 and svc.final_veto
    day = next(iter(svc.journal.days))
    key = day+"/"+svc.journal.boot
    # The read boundary drops the first exact reply; no full scan or retry.
    read = svc.store.read
    reads = []
    def first_miss(kind, requested, budget):
        reads.append((kind, requested))
        assert kind == CLOSURE_KIND and requested == key
        return None
    with monkeypatch.context() as local:
        local.setattr(svc.store, "read", first_miss)
        assert not svc.store.reconcile([svc.unknown[3]], Budget(104., svc.store.clock))
    assert reads == [(CLOSURE_KIND, key)]
    with rpc_scope(104., 2., svc.store.clock):
        commit(*pending[0][0], **pending[0][1])
    assert read(CLOSURE_KIND, key, Budget(104., svc.store.clock)) is not None
    svc.flush(104.)
    assert day in svc.final_veto and len(pending) == 1
    assert read(CLOSE_RESULT_KIND, key, Budget(104., svc.store.clock)) is None
