"""Round six: startup output, producer totals, retained inputs and clock bounds."""
from __future__ import annotations

import asyncio
import copy
import logging
import threading
import time
from dataclasses import asdict, replace

import pytest
from fastapi import FastAPI

from tests.test_async_settle_shadow import NOW, context
from tests.test_async_settle_shadow_cohort import additions, config
from tests.test_async_settle_shadow_cohort_runtime import installed, request
from tests.test_async_settle_shadow_startup_harness import descriptor, fake_harness
from tests.test_async_settle_ticket import endpoint
from trusted_router import async_settle_shadow_v2_collect_ports as collector
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services import async_settle_shadow_startup_harness as h
from trusted_router.services import async_settle_shadow_startup_records as records


def stop(shell, service):
    shell.close()
    shell.worker.join(2)
    service.publisher_executor.shutdown(wait=True)
    service.evidence_executor.shutdown(wait=True)


async def test_logging_setup_does_not_hold_listener(monkeypatch):
    app = FastAPI()
    h.install(app, config(source_revision="a" * 40))
    startup = app.state.async_settle_shadow_startup
    monkeypatch.setattr(h, "read_fd", descriptor)
    def missing(path):
        raise FileNotFoundError
    monkeypatch.setattr(h, "load_manifest", missing)
    @app.get("/live")
    async def live():
        return {"live": True}
    entered = threading.Event()
    def hold():
        logging._acquireLock()
        try:
            entered.set()
            time.sleep(.4)
        finally:
            logging._releaseLock()
    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(1)
    async def probe():
        started = time.monotonic()
        await asyncio.sleep(.02)
        messages = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            messages.append(message)
        await app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "method": "GET", "scheme": "http", "path": "/live", "raw_path": b"/live",
            "query_string": b"", "headers": [], "server": ("test", 80),
            "client": ("test", 1), "root_path": ""}, receive, send)
        assert messages[0]["status"] == 200
        return time.monotonic() - started
    task = asyncio.create_task(probe())
    h.listener_started(app)
    try:
        assert await task < .2
        await startup.task
        assert not startup.admission.ready
    finally:
        holder.join(1)
        # A timed-out setup retains its one worker slot until the lock is free.
        for _ in range(200):
            if h._STARTUP_WORKER.acquire(blocking=False):
                h._STARTUP_WORKER.release()
                break
            await asyncio.sleep(.005)
        else:
            pytest.fail("startup worker did not finish")


async def test_prepared_first_emit_never_configures_logging(monkeypatch):
    emitter = records.Emitter()
    await h.startup_call(emitter.prepare, .3)
    write = emitter.write
    def forbidden(*args, **kwargs):
        raise AssertionError("first emit configured output")
    with monkeypatch.context() as scoped:
        scoped.setattr(logging, "getLogger", forbidden)
        scoped.setattr(records, "stdout_writer", forbidden)
        emitter.prepare()
        assert emitter.write is write
        assert emitter.emit(dict(kind="a3_traffic_ready", boot="boot", at_us=1)) is not None
        assert emitter.emitted == 1 and emitter.dropped == 0


def test_unprepared_output_drops_without_setup(monkeypatch):
    def forbidden():
        pytest.fail("emit started output setup")
    monkeypatch.setattr(records, "stdout_writer", forbidden)
    emitter = records.Emitter()
    assert emitter.emit(dict(kind="a3_traffic_ready", boot="boot", at_us=1)) is None
    assert emitter.dropped == 1


@pytest.mark.parametrize("busy", ["mutex", "full"])
def test_output_queue_never_waits(busy):
    queue = records.OutputQueue(1)
    emitter = records.Emitter(queue.put_nowait)
    if busy == "full":
        queue.put_nowait(b"earlier")
    else:
        queue.mutex.acquire()
    try:
        started = time.monotonic()
        assert emitter.emit(dict(kind="a3_traffic_ready", boot="boot", at_us=1)) is None
        assert time.monotonic() - started < .1
        assert emitter.dropped == 1
    finally:
        if busy == "mutex":
            queue.mutex.release()


@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("refusal", ["rate_limit", "queue_full", "dispatch", "no_slot_and_rate_limit", "no_slot", "none"])
def test_producer_counts_survive_refused_work(monkeypatch, ready, refusal):
    shell, service, _ = installed()
    startup, _, _ = fake_harness()
    startup.boot = service.journal.boot
    h.bind_service(startup, service, shell)
    shell.startup_admission = startup.admission
    startup.admission.ready = ready
    monkeypatch.setattr("time.time_ns", lambda: NOW * 10**9)
    if refusal in {"rate_limit", "no_slot_and_rate_limit"}:
        shell.tokens, shell.refilled = 0, time.monotonic() + 100
    elif refusal == "queue_full":
        shell.pending = 32
    elif refusal == "dispatch":
        monkeypatch.setattr(shell, "dispatch", lambda *args: False)
    if refusal.startswith("no_slot"):
        service.journal.permits[next(iter(service.journal.days))] = 0
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash="not-retained", model=ctx.authorization.model_id,
        route_type="chat.completions", stream=False, requested_parameters=[])
    try:
        shell.authorize_http(request([("X-TR-Settlement-Shadow-Policy", "shadow-v2")]),
            body, ctx.authorization, additions(), [endpoint()], typed=True, federated=False, replay=False)
        shell.join()
        assert startup.attempt_count == startup.missing_attempts == shell.opted_starts == 1
        assert shell.opted_consumed == int(refusal in {"none", "no_slot"})
        assert service.port.lost.is_set() == (refusal != "none")
        if refusal == "none":
            service.journal.freeze(next(iter(service.journal.days)))
            service.flush(service.store.clock() + 2)
            assert startup.attempt_count == startup.attempt_projected == 1
            assert startup.missing_attempts == 0
    finally:
        stop(shell, service)


def test_queue_retains_only_charged_completion_fields(monkeypatch):
    shell, service, _ = installed()
    entered, release = threading.Event(), threading.Event()
    def hold():
        entered.set()
        release.wait(5)
    shell.dispatch(hold)
    assert entered.wait(1)
    captured = []
    dispatch = shell.dispatch
    def transfer(action, *args):
        if action == shell.input_work:
            captured.append(args)
        return dispatch(action, *args)
    monkeypatch.setattr(shell, "dispatch", transfer)
    monkeypatch.setattr("time.time_ns", lambda: NOW * 10**9)
    ctx = context()
    endpoints = [replace(endpoint(), id=f"e{i:03d}" + "x" * 124) for i in range(64)]
    auth = replace(ctx.authorization, candidate_endpoint_ids=[e.id for e in endpoints])
    body = GatewayAuthorizeRequest(api_key_hash="not-retained", model=auth.model_id,
        route_type="chat.completions", stream=False, requested_parameters=[])
    try:
        for _ in range(10):
            shell.authorize_http(request([("X-TR-Settlement-Shadow-Policy", "shadow-v2")]),
                body, auth, additions(), endpoints, typed=True, federated=False, replay=False)
        assert len(captured) == shell.pending == 10
        total = 0
        for inputs, capable, charge in captured:
            assert capable and inputs.event["diagnostic"] is not None
            assert inputs.event["candidates"] is None
            assert inputs.server_candidates is inputs.server_facts is inputs.facts is inputs.candidates is None
            assert inputs.identity == inputs.dimensions == {}
            encoded = records.canonical(asdict(inputs))
            assert charge == len(encoded)
            total += len(encoded)
        assert shell.queued_bytes == 32 * 4096 + total <= 256 * 1024
        # The same allowance still refuses even a one-byte addition at its cap.
        shell.tokens, shell.queued_bytes = 10, 256 * 1024
        assert shell.admit(1) == "queue_full"
        shell.queued_bytes = 32 * 4096 + total
    finally:
        release.set()
        shell.join()
        stop(shell, service)


def observer_record():
    def ref(field):
        return records.coordinate(field, b"original")
    return dict(kind="a3_boot_check", boot="boot", role="router", revision="a" * 40,
        region="us-central1", boot_at_us=0, criterion="observer_freshness",
        completed_at_us=records.MAX_INT, checks=dict.fromkeys(records.CHECKS["observer_freshness"], "pass"),
        config_sha256="a" * 64, inputs={}, clock_basis=dict(clock="utc_ns", boot="boot", boot_at_us=0,
        original=ref("clock_basis.original")), health_basis=ref("health_basis"), install_elapsed_us=0,
        contender=True, installed_at_ns=records.MAX_INT, claim=ref("claim"), publication=ref("publication"),
        publication_elapsed_us=0, claim_at_ns=records.MAX_INT, published_at_ns=records.MAX_INT)


@pytest.mark.parametrize("field", ["installed_at_ns", "published_at_ns", "claim_at_ns"])
@pytest.mark.parametrize("over", [False, True])
def test_nanosecond_ceiling_matches_collector(field, over):
    value = observer_record()
    # Preserve the relative ordering, so the fixed ceiling is the decisive rule.
    names = ["installed_at_ns", "published_at_ns", "claim_at_ns"]
    for name in names[:names.index(field) + 1]:
        value[name] += int(over)
    lines = []
    emitter = records.Emitter(lines.append)
    raw = emitter.emit(value)
    collected = copy.deepcopy(value)
    for name in ("health_basis", "claim", "publication"):
        collected[name] = dict(path="original.json", sha256="a" * 64, bytes=8)
    collected["clock_basis"]["original"] = dict(path="original.json", sha256="a" * 64, bytes=8)
    if over:
        with pytest.raises(ValueError):
            collector.runtime_record(collected)
        assert raw is None and not lines and emitter.dropped == 1 and emitter.emitted == 0
    else:
        collector.runtime_record(collected)
        assert lines == [raw] and emitter.emitted == 1 and emitter.dropped == 0


@pytest.mark.parametrize("value,minimum,maximum,accepted", [
    (0, -1, records.MAX_INT + 1, True), (-1, -1, records.MAX_INT + 1, False),
    (records.MAX_INT, 0, records.MAX_INT + 1, True),
    (records.MAX_INT + 1, 0, records.MAX_INT + 1, False), (2, 0, 1, False), (0, 1, 2, False),
])
def test_integer_intersects_all_bounds(value, minimum, maximum, accepted):
    if accepted:
        records.integer(value, minimum, maximum)
    else:
        with pytest.raises(ValueError):
            records.integer(value, minimum, maximum)
