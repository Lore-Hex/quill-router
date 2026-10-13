"""P3 native serialization and slot races, selected by CI's emulator command."""
from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from typing import Any
from uuid import uuid4

import pytest

from tests.shadow_observation_diagnostics import NativeLiveness, client_history, status
from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import NOW_US, maximum_rows
from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore

pytestmark = [pytest.mark.xdist_group("conformance-spanner-emulator"), pytest.mark.timeout(30)]


def _diagnostics():
    return "no service constructed"


@pytest.fixture(autouse=True)
def native_diagnostics(monkeypatch, native_watchdog, package_hygiene):  # noqa: F811
    from trusted_router.services.async_settle_shadow_health import Service
    global _diagnostics
    services, stores = [], []
    initialize_store, initialize_service = ObservationStore.__init__, Service.__init__
    with ExitStack() as stack:
        def store_init(store, *args, **kwargs):
            initialize_store(store, *args, **kwargs)
            stores.append((store, stack.enter_context(client_history(store))))
        def service_init(svc, *args, **kwargs):
            initialize_service(svc, *args, **kwargs)
            services.append(svc)
        monkeypatch.setattr(ObservationStore, "__init__", store_init)
        monkeypatch.setattr(Service, "__init__", service_init)
        def diagnostic():
            return "\n".join([status(svc, next(h for st, h in stores if st is svc.store)) for svc in services]
                             + [repr(dict(operations=h, wall_us=int(st.wall()*1e6), mono=st.clock(),
                                          lifecycle=None, journal=None, final_interval=None))
                                for st, h in stores if all(st is not s.store for s in services)])
        _diagnostics = diagnostic
        try:
            yield
        finally:
            print("NATIVE_STATUS", diagnostic())
            for svc in services:
                svc.publisher_executor.shutdown(wait=True, cancel_futures=True)
                svc.evidence_executor.shutdown(wait=True, cancel_futures=True)


@pytest.fixture(scope="session")
def native_liveness(request):
    state = NativeLiveness()
    class Reports:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_makereport(self, item, call):
            result = yield
            state.report(result.get_result())
    request.config.pluginmanager.register(Reports(), "p3-native-liveness")
    return state


@pytest.fixture(autouse=True)
def native_watchdog(request, native_liveness):
    # sys.__stderr__ still uses fd 2 under pytest fd capture. Suspend both
    # capture layers for the flushed marker so cancellation cannot hide it.
    capture = request.config.pluginmanager.getplugin("capturemanager")
    with capture.global_and_fixture_disabled():
        native_liveness.start(request.node.nodeid)


@pytest.fixture(scope="module")
def native_resources_pool():
    from tests.shadow_observation_fixtures import native_rpc_deadlines
    with native_rpc_deadlines(), ExitStack() as stack:
        yield stack, []


@pytest.fixture
def shadow_emulator_resources(request, native_watchdog, native_resources_pool, package_hygiene):  # noqa: F811
    # Use the conformance helper's disposable schema. Every death history owns
    # a distinct database/instance; no dead-session deletion or cross-history RW
    # contention. Ordinary histories share one lazily provisioned schema.
    from tests.conformance.spanner_emulator import emulator_resources
    if "SPANNER_EMULATOR_HOST" in os.environ:
        assert os.environ.get("TR_CONFORMANCE_EMULATOR_SCHEMA") == "1", "configured emulator requires schema coverage"
    stack, shared = native_resources_pool
    try:
        if "process_restart" in request.node.name:
            with emulator_resources() as resources:
                try:
                    yield resources
                finally:
                    resources[0].drop()
        else:
            if not shared:
                shared.append(stack.enter_context(emulator_resources()))
                stack.callback(shared[0][0].drop)
            yield shared[0]
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
    assert len(captured) == 1, _diagnostics()
    native = captured[0]
    fake = Transaction(SimpleNamespace())
    store.write(fake, [*pages, counter, closure], Budget(time.monotonic()+4))
    assert list(native.mutations) == list(fake._mutations), _diagnostics()
    assert sum(len(row.body) for row in [*pages, counter, closure]) == 331776, _diagnostics()
    assert max(len(row.key) for row in pages) == 96, _diagnostics()
    assert 331776 < len(CommitRequest.serialize(native)) < 336000, _diagnostics()
    assert store.reconcile([*pages, counter, closure], Budget(time.monotonic()+4)), _diagnostics()


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
            assert contender_store.publish(value, budget), _diagnostics()
            return value
        except Exception:
            return None
    # The winner observes immediately, without waiting for the loser's timeout.
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(contender, "a", store)
        b = pool.submit(contender, "b", other)
        winners = [value for value in (a.result(), b.result()) if value is not None]
    assert len(winners) == 1, _diagnostics()
    value = winners[0]
    read = store.read_shadow_health()
    assert read is not None and read["body"] == value, _diagnostics()
    assert value["observed_at_ns"] <= read["committed_at_ns"] <= read["read_at_ns"], _diagnostics()
    with pytest.raises(ValueError, match="a1-server-slot"):
        store.claim("future", slot+100, now_us+200_000_000, Budget(time.monotonic()+.5))
    assert not store.publish(value, Budget(time.monotonic()+.5)), _diagnostics()  # Same slot cannot overwrite.


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
    assert svc.store.reconcile([newer], Budget(time.monotonic()+4)), _diagnostics()


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
    assert r is not None, _diagnostics()
    assert svc.port.finalize(r, event_for(r)), _diagnostics()
    assert svc.port.complete(r), _diagnostics()
    svc.flush(time.monotonic()+4)
    assert svc.journal.days[r.day].hwm == 1, _diagnostics()


@pytest.mark.parametrize("backend", ["spanner-emulator"])
def test_shadow_observation_native_empty_nonempty_metadata(shadow_emulator_resources: Any, backend: str) -> None:
    import datetime as dt

    from google.cloud.spanner_v1 import KeySet
    database, _ = shadow_emulator_resources
    store = ObservationStore(database)
    slot = int(time.time())//2
    empty = store.observe("reader", "us-central1", "a"*40, slot, Budget(time.monotonic()+.5))
    assert empty["observed_at_ns"] > 0 and empty["backlog_count"] == 0, _diagnostics()
    identity = uuid4().hex
    with database.batch() as batch:
        batch.insert(table="tr_settle_outbox", columns=("authorization_id", "intent_kind", "settle_origin", "actual_cost_micro", "status", "created_at"),
            values=[(identity, "settle", "typed", 1, "dead", dt.datetime.now(dt.UTC))])
    try:
        nonempty = store.observe("reader", "us-central1", "a"*40, slot, Budget(time.monotonic()+.5))
        assert nonempty["observed_at_ns"] > empty["observed_at_ns"], _diagnostics()
        assert nonempty["backlog_count"] == empty["backlog_count"]+1, _diagnostics()
        assert nonempty["dead_count"] == empty["dead_count"]+1, _diagnostics()
    finally:
        with database.batch() as batch:
            batch.delete("tr_settle_outbox", KeySet(keys=[(identity, "settle")]))


def native_service(database):
    from tests.shadow_observation_fixtures import observation_service
    return observation_service(ObservationStore(database, wall=lambda: NOW_US/1e6+100))


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
def test_shadow_native_exact_miss_then_late_landing(shadow_emulator_resources, backend, monkeypatch):
    from google.api_core.exceptions import DeadlineExceeded
    from google.cloud.spanner_v1.types import CommitRequest

    from trusted_router.async_settle_shadow_journal import CLOSE_RESULT_KIND, CLOSURE_KIND
    from trusted_router.storage_gcp_shadow_io import rpc_scope
    database, _ = shadow_emulator_resources
    svc = native_service(database)
    now = [100.]
    from tests.shadow_observation_fixtures import bind_clock
    bind_clock(svc, lambda: now[0])
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
    assert len(pending) == 1 and svc.final_veto, _diagnostics()
    day = next(iter(svc.journal.days))
    key = day+"/"+svc.journal.boot
    # The read boundary drops the first exact reply; no full scan or retry.
    read = svc.store.read
    reads = []
    def first_miss(kind, requested, budget):
        reads.append((kind, requested))
        assert kind == CLOSURE_KIND and requested == key, _diagnostics()
        return None
    with monkeypatch.context() as local:
        local.setattr(svc.store, "read", first_miss)
        assert not svc.store.reconcile([svc.unknown[3]], Budget(104., svc.store.clock)), _diagnostics()
    assert reads == [(CLOSURE_KIND, key)], _diagnostics()
    with rpc_scope(104., 2., svc.store.clock):
        commit(*pending[0][0], **pending[0][1])
    assert read(CLOSURE_KIND, key, Budget(104., svc.store.clock)) is not None, _diagnostics()
    svc.flush(104.)
    assert day in svc.final_veto and len(pending) == 1, _diagnostics()
    assert read(CLOSE_RESULT_KIND, key, Budget(104., svc.store.clock)) is None, _diagnostics()


@pytest.mark.parametrize("backend", ["spanner-emulator"])
@pytest.mark.timeout(60)
@pytest.mark.parametrize("signum", ["SIGTERM", "SIGKILL"])
@pytest.mark.parametrize("point", ["before_first_flush", "graceful", "after_result", "before_result", "after_ack", "before_ack", "request_sent", "full_queues", "during_commit", "request_pending_drop"])
def test_shadow_native_process_restart(shadow_emulator_resources, backend, point, signum, tmp_path):
    import json
    import signal
    import subprocess
    import sys
    from pathlib import Path

    from tests.shadow_observation_diagnostics import wait_boundary
    database, _ = shadow_emulator_resources
    boot = uuid4().hex
    args = [sys.executable, str(Path(__file__).parents[1]/"shadow_observation_restart.py"),
        database._instance._client.project, database._instance.instance_id, database.database_id, boot]
    manifest = str(tmp_path/"manifest.json")
    log = tmp_path/"child.log"
    output = []
    pending = point.startswith("request_pending")
    gate = None
    child_env = dict(os.environ)
    if pending:
        from tests.shadow_observation_gate import CommitGate
        gate = CommitGate(os.environ["SPANNER_EMULATOR_HOST"])
        child_env["SPANNER_EMULATOR_HOST"] = gate.address
    with log.open("w") as errors:
        child = subprocess.Popen([*args, point, manifest], stdout=subprocess.PIPE, stdin=subprocess.PIPE, stderr=errors, bufsize=0, env=child_env)  # noqa: S603
        def child_state():
            return "\n".join(output)+"\nSTDERR\n"+log.read_text()+"\nPARENT\n"+_diagnostics()
        try:
            if gate is not None:
                assert wait_boundary(child.stdout, output, time.monotonic()+15, marker=b"GATE_ARM"), child_state()
                gate.arm()
                child.stdin.write(b"1")
                child.stdin.flush()
                assert gate.pending.wait(3), child_state()
                assert gate.buffer, "commit must be outstanding in the transport gate"
            else:
                assert wait_boundary(child.stdout, output, time.monotonic()+15), child_state()
            child.send_signal(getattr(signal, signum))
            code = child.wait(timeout=12)
            output.extend(child.stdout.read().decode().splitlines())
            assert code == (0 if signum == "SIGTERM" else -signal.SIGKILL), child_state()
            if signum == "SIGTERM" and not pending:
                shutdown = json.loads(output[-1])
                assert shutdown["handler_called"] and shutdown["shutdown"], child_state()
                assert shutdown["status"]["unclosed"] == (point != "graceful"), child_state()
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            if gate is not None:
                gate.close()
            print("CHILD_DIAGNOSTICS", child_state())
    if pending:
        assert not json.loads(Path(manifest).read_text()).get("pending_call_returned", False), child_state()
    recovered = subprocess.run([*args, "recover", manifest], capture_output=True, text=True, timeout=15)  # noqa: S603
    diagnostics = child_state()+"\nRECOVERY\n"+recovered.stdout+recovered.stderr
    assert recovered.returncode == 0, diagnostics
    state = json.loads(recovered.stdout.splitlines()[-1])
    durable = point == "after_result" or (point == "graceful" and signum == "SIGTERM")
    assert state["durable"] == durable and state["missing_close"] != durable, diagnostics
    assert state["result_attempts"] == int(durable), diagnostics
    if pending:
        assert state["writer_closure_gap"] and not state["original_acknowledgement"], diagnostics
        assert state["final_attempts"] == 1 and state["result_attempts"] == 0, diagnostics
        assert not state["closure"], diagnostics
    else:
        assert state["closure"] == (durable or point in {"request_sent", "before_ack", "after_ack", "before_result"}), diagnostics
    if point == "full_queues":
        assert state["sticky_loss"] and state["missing_close"], diagnostics
        memory = state["lost_memory"]
        assert {k: memory[k] for k in ("pages", "retained", "basis", "trace", "observers", "publishers")} == dict(
            pages=4, retained=32, basis=4, trace=4, observers=32, publishers=1), diagnostics
        assert memory["work_bytes"] == 256*1024, diagnostics
        assert state["hwm"] == 0 and state["event_seq"] > 32 and len(state["lost_attachments"]) == 8, diagnostics
        assert len(state["found"]) == 6 and state["found"] == state["landed"], diagnostics
