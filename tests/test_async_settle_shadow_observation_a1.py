"""A1 review witnesses through the service and native SDK boundaries."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import Aborted, DeadlineExceeded, InternalServerError

from tests.test_async_settle_shadow_observation import (
    NOW_US,
    event_for,
    health,
    installed,
    service,
)
from tests.test_async_settle_shadow_v2_evidence import exhaustive, untraced  # noqa: F401
from trusted_router.async_settle_shadow_attachments import Lane
from trusted_router.async_settle_shadow_journal import (
    CLOSE_RESULT_KIND,
    CLOSURE_KIND,
    RESERVATION_BLOCK,
)
from trusted_router.async_settle_shadow_v2_a1_health import validate_publisher_final
from trusted_router.storage_gcp_shadow_observation import Budget, ObservationStore, UnknownCommit


@exhaustive
def test_dormant_adapter_exact_base_and_rpc_call_allocation_differential(tmp_path):
    from trusted_router import storage_gcp_io as head
    raw = (Path(__file__).parent/"fixtures/storage_gcp_io_main_9135f4b7.py.txt").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "9db65df2443e2a8820d09e3d19e992ab9d60c42233021d0c6fccafbed034cbb5"
    assert Path(head.__file__).read_bytes() == raw
    path = tmp_path/"base_io.py"
    path.write_bytes(raw)
    spec = importlib.util.spec_from_file_location("base_io", path)
    base = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = base
    spec.loader.exec_module(base)
    import contextvars
    # Baseline code runs with the identical caller ContextVar keys. Fresh keys
    # have randomized hashes and different HAMT allocation shapes despite
    # identical functions, which would measure the harness rather than P3.
    for name, value in vars(head).items():
        if isinstance(value, contextvars.ContextVar):
            setattr(base, name, value)
    methods = ("begin_transaction", "commit", "execute_sql", "execute_batch_dml", "execute_streaming_sql",
        "read", "streaming_read", "rollback", "create_session", "batch_create_sessions", "get_session", "list_sessions", "delete_session")
    for module in (base, head):
        tree = ast.parse(Path(module.__file__).read_text())
        assert not any(isinstance(n, ast.Name) and n.id in {"_RPC_OBSERVER", "_SPANNER_NO_RETRY"} for n in ast.walk(tree))
    def run(module, method, error, cap, *, allocations=False):
        calls, frames = [], []
        def rpc(*args, **kwargs):
            calls.append((args, kwargs))
            if error:
                raise error
            return ("response",)
        db = SimpleNamespace(spanner_api=SimpleNamespace(**dict.fromkeys(methods, rpc)),
                             run_in_transaction=lambda fn, **kw: fn(kw))
        old_time = module.time
        module.time = SimpleNamespace(monotonic=lambda: 100.)
        module.configure_spanner_rpc_deadlines(db)
        def profile(frame, event, arg):
            if event == "call" and frame.f_globals.get("__name__") == module.__name__:
                frames.append((frame.f_code.co_name, frame.f_code.co_code))
        previous = sys.getprofile()
        sys.setprofile(profile)
        if allocations:
            tracemalloc.start()
        try:
            with module.spanner_rpc_deadline(101., max_rpc_seconds=cap), module.count_spanner_rpcs() as count:
                try:
                    result = getattr(db.spanner_api, method)(123, timeout=.75, retry=None)
                except Exception as exc:
                    result = (type(exc).__name__, str(exc))
                output = (calls, result, count.value(), frames)
                if allocations:
                    blocks = sorted((v.traceback[0].lineno, v.count, v.size)
                        for v in tracemalloc.take_snapshot().statistics("lineno")
                        if v.traceback[0].filename == module.__file__)
                    return output, blocks
                return output
        finally:
            sys.setprofile(previous)
            module.time = old_time
    import json
    import tracemalloc
    allocation_counts = []
    def measured(module, method, error_type, message, cap):
        error = error_type(message) if error_type else None
        try:
            return run(module, method, error, cap, allocations=True)
        finally:
            tracemalloc.stop()
    for method in methods:
        for error_type, message in ((None, ""), (Aborted, "x"), (InternalServerError, "x"),
                                    (InternalServerError, "RST_STREAM"), (DeadlineExceeded, "x")):
            for cap in (None, .5):
                # Warm both code objects before counting retained native/Python blocks.
                for module in (base, head):
                    run(module, method, error_type(message) if error_type else None, cap)
                baseline = measured(base, method, error_type, message, cap)
                current = measured(head, method, error_type, message, cap)
                assert baseline == current
                allocation_counts.append(dict(method=method, error=error_type.__name__ if error_type else None,
                    message=message, cap=cap, retained_blocks=sum(v[1] for v in current[1]),
                    retained_bytes=sum(v[2] for v in current[1])))
    (tmp_path/"allocation-differential.json").write_text(json.dumps(allocation_counts))
    # Live blocks attributed to the adapter are measured at the return boundary;
    # exact source/bytecode equality additionally covers freed transient work.


def test_generic_completion_missing_original_stays_unresolved():
    svc = service()
    svc.journal.grant("2026-10-12", RESERVATION_BLOCK)
    reservation = svc.port.reserve(NOW_US)
    original = event_for(reservation)
    event = dict(original, phase="completion", receipt_locator=None, completion_of="00000000000000000099",
        retry_original=dict(day=reservation.day, boot=reservation.boot, event_seq="00000000000000000099"),
        proof_kind="none", comparison=None, prediction=None, certificate_sha256=None, original_receipt=None,
        payload_sha256=None, booking=dict(outcome="not_attempted", reference=None, winner=None))
    assert svc.port.finalize(reservation, event)
    assert svc.port.complete(reservation)
    svc.flush(time.monotonic()+4)
    assert svc.journal.days[reservation.day].hwm == 0
    assert reservation in svc.journal.batches
    page = svc.journal.pages[0].row
    assert page is not None
    with pytest.raises(ValueError, match="retry_original_gap"):
        svc.journal.verify_complete(reservation, [page])
    assert svc.journal.days[reservation.day].hwm == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["error", "unknown", "late"])
async def test_final_original_failure_permanently_vetoes_result(monkeypatch, fault):
    svc = service()
    real = svc.store.flush
    calls = []
    def flush(*args):
        calls.append(args)
        real(*args)
        if fault == "unknown":
            raise UnknownCommit("lost reply")
        if fault == "error":
            raise RuntimeError("error")
        svc.store.clock = lambda: args[-1].deadline+1
    monkeypatch.setattr(svc.store, "flush", flush)
    await svc.close()
    assert svc.unclosed and svc.final_veto == {"2026-10-12"}
    assert len(calls) == 1
    assert not any(kind == CLOSE_RESULT_KIND for kind, _ in svc.store.database.rows)
    assert any(kind == CLOSURE_KIND for kind, _ in svc.store.database.rows)


@pytest.mark.asyncio
async def test_handoff_lost_reply_landed_one_attempt(monkeypatch):
    svc = service()
    real = svc.store.close_result
    attempts = []
    def lost(row, budget):
        attempts.append(row)
        real(row, budget)
        raise UnknownCommit("result reply lost")
    monkeypatch.setattr(svc.store, "close_result", lost)
    await svc.close()
    assert not svc.unclosed and not svc.final_veto and len(attempts) == 1
    assert attempts[0].value()["shutdown_remaining_us"] is not None


def test_attempted_unknown_claim_is_failure(monkeypatch):
    svc = service()
    svc.store.wall = lambda: NOW_US/1e6
    def fail(*args):
        raise UnknownCommit("claim")
    monkeypatch.setattr(svc.store, "claim", fail)
    svc.publish_cycle(NOW_US//2_000_000, NOW_US)
    counts = svc.counters["2026-10-12"]["publisher"]
    validate_publisher_final(counts)
    assert counts["claim_attempts"] == counts["failures"] == counts["unknown_outcomes"] == 1
    assert counts["missed_slots"] == 0


def test_preclaim_lock_wait_rechecks_jitter(monkeypatch):
    svc = service()
    wall = [NOW_US]
    svc.store.wall = lambda: wall[0]/1e6
    calls = []
    monkeypatch.setattr(svc.store, "claim", lambda *a: calls.append(a))
    with svc.lock:
        svc.schedule_cycle(NOW_US//2_000_000, NOW_US)
        job = svc.publisher_executor.submit(svc.publish_cycle, NOW_US//2_000_000, NOW_US)
        wall[0] += 300_000
    job.result(2)
    assert calls == []
    counts = svc.counters["2026-10-12"]["publisher"]
    validate_publisher_final(counts)
    assert counts["missed_slots"] == 1
    svc.publisher_executor.shutdown()


def test_midnight_queued_cycle_owns_day_before_executor_start():
    svc = service()
    wall = [NOW_US+86_398_000_000]
    svc.store.wall = lambda: wall[0]/1e6
    svc.observer.wall = svc.observer.evidence_wall = svc.store.wall
    svc.observer.clock = lambda: 0.
    svc.observer.daily_since = 0.
    release = threading.Event()
    blocker = svc.publisher_executor.submit(release.wait, 3)
    stamp = wall[0]
    with svc.lock:
        svc.schedule_cycle(stamp//2_000_000, stamp)
        job = svc.publisher_executor.submit(svc.publish_cycle, stamp//2_000_000, stamp)
    wall[0] += 3_000_000
    svc.flush(time.monotonic()+4)
    assert "2026-10-12" in svc.journal.days
    release.set()
    blocker.result(2)
    job.result(2)
    counts = svc.counters["2026-10-12"]["publisher"]
    validate_publisher_final(counts)
    assert counts["missed_slots"] == 1
    assert svc.traces.pending("2026-10-12")
    svc.publisher_executor.shutdown()


class MemoryAttachments:
    def __init__(self):
        self.objects = {}
        self.calls = []
        self.lose = False
    def put(self, key, body, timeout):
        self.calls.append(("put", key, len(body)))
        assert key not in self.objects
        self.objects[key] = body
        if self.lose:
            raise TimeoutError("lost")
    def get(self, key, timeout):
        self.calls.append(("get", key))
        return self.objects.get(key)


def basis():
    from trusted_router.async_settle_shadow_health_state import health_basis
    svc = service()
    svc.observer.clock = lambda: 10.
    svc.observer.health = installed(health())
    return health_basis(svc.observer, "issuer")


@exhaustive
def test_attachment_queue_token_unknown_and_churn_product():
    for boots in (1, 2, 8):
        for lose in (False, True):
            sink = MemoryAttachments()
            sink.lose = lose
            lanes = [Lane("basis", f"b{i}", sink) for i in range(boots)]
            clock = [0.]
            for lane in lanes:
                value = dict(basis(), reader_boot=lane.boot)
                lane.append("2026-10-12", value)
                for now in (0., 0., 4.999999, 5.):
                    clock[0] = now
                    lane.step(Budget(now+2, lambda clock=clock: clock[0]))
                    assert lane.operations["get"] == int(lose and now >= 5.)
                assert not lane.queue
                assert lane.operations["put"] == 1
                assert lane.operations["get"] == int(lose)
            assert len(sink.calls) == boots*(1+lose)
    lane = Lane("basis", "issuer", None)
    value = basis()
    for q in range(40):
        lane.append("2026-10-12", dict(value, since_reply_ns=q))
    assert len(lane.queue) == 4 and lane.retained_bytes <= 256*1024
    with pytest.raises(ValueError, match="overflow"):
        lane.append("2026-10-12", dict(value, since_reply_ns=40))
    assert lane.blocked and lane.pending("2026-10-12")


def test_health_reader_monotonic_budget_and_no_wall_validity():
    svc = service()
    svc.observer.clock = lambda: 10.
    svc.observer.wall = lambda: -10**9
    svc.observer.health = installed(health())
    from trusted_router.services.async_settle import Admission
    svc.observer.install_workspace("ws", 10., Admission(0, 2))
    assert svc.observer.peek("ws")["prediction"] == "yes"
    svc.observer.clock = lambda: 11.500000001
    assert svc.observer.peek("ws")["prediction"] == "unknown"


@pytest.mark.parametrize("consumer", ["observe", "health", "evidence"])
@pytest.mark.parametrize("checkout", ["empty", "expired", "expired_session", "contended"])
def test_native_checkout_inside_owning_budget(monkeypatch, consumer, checkout):
    from google.cloud.spanner_v1.database import SnapshotCheckout
    from google.cloud.spanner_v1.types import PartialResultSet
    from google.cloud.spanner_v1.types import Session as SessionPB

    from tests.test_async_settle_shadow_observation import sdk_database
    from trusted_router.storage_gcp_shadow_io import Sessions
    clock = SimpleNamespace(now=10.)
    clock.monotonic = lambda: clock.now
    # The base SDK fixture only needs this clock and pytest's patch facility.
    db, _, _ = sdk_database.__wrapped__(monkeypatch, clock)
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        assert kwargs["retry"] is None
        return SessionPB(name=db.name+"/sessions/new")
    db.database_role = None
    db.spanner_api.create_session = create
    def query(**kwargs):
        calls.append(kwargs)
        if checkout == "expired_session" and len(calls) == 1:
            from google.api_core.exceptions import NotFound
            raise NotFound("expired session")
        response = PartialResultSet(metadata=dict(transaction=dict(read_timestamp="2026-10-12T00:00:00.000000123Z"),
            row_type=dict(fields=[dict(name="body", type=dict(code="STRING"))])))
        return iter([response])
    db.spanner_api.execute_streaming_sql = query
    db.sessions_manager = Sessions(db)
    db.snapshot = lambda: SnapshotCheckout(db)
    store = ObservationStore(db, clock=clock.monotonic, wall=lambda: NOW_US/1e6)
    cap = .25 if consumer == "health" else .5 if consumer == "observe" else 1.
    if checkout == "expired_session":
        db._sessions_manager = db.sessions_manager
        db.sessions_manager.session = db.session
    if checkout == "expired":
        clock.now += 2
    if checkout == "contended":
        db.sessions_manager.lock.acquire()
        # Use a tiny but positive native checkout budget; no unbounded pool wait.
        cap = .001
    budget = Budget(10.+cap, clock.monotonic)
    def read():
        if consumer == "observe":
            return store.observe("issuer", "us-central1", "a"*40, NOW_US//2_000_000, budget)
        if consumer == "evidence":
            return store.read(CLOSURE_KIND, "2026-10-12/issuer", budget)
        # The public reader starts its own 250ms budget before checkout.
        return store.read_shadow_health()
    try:
        if checkout == "expired_session":
            with pytest.raises(RuntimeError):
                read()
            assert len(calls) == 1 and db.sessions_manager.session is None
            read()
            assert len(calls) == 3  # A later operation gets a fresh session; no hidden retry.
        elif checkout == "expired" and consumer != "health" or checkout == "contended":
            with pytest.raises((TimeoutError, RuntimeError)):
                read()
            assert not calls
        else:
            result = read()
            if consumer == "observe":
                assert result["observed_at_ns"] == NOW_US*1000+123
            assert calls and all(0 < c["timeout"] <= cap for c in calls)
    finally:
        if checkout == "contended":
            db.sessions_manager.lock.release()


@pytest.mark.asyncio
async def test_authorize_receipt_closure_commits_open_report_obligation():
    from trusted_router.async_settle_shadow_journal import PAGE_KIND
    from trusted_router.async_settle_shadow_v2_a1_reconciliation import authorization_obligation
    svc = service()
    svc.journal.grant("2026-10-12", RESERVATION_BLOCK)
    r = svc.port.reserve(NOW_US)
    assert svc.port.finalize(r, event_for(r)) and svc.port.complete(r)
    await svc.close()
    assert not svc.unclosed
    page = svc.store.read(PAGE_KIND, f"{r.day}/{r.boot}/{r.first_seq:020}", Budget(time.monotonic()+1))
    closure = svc.store.read(CLOSURE_KIND, r.day+"/"+r.boot, Budget(time.monotonic()+1))
    assert closure.value()["authorization_obligations"] == "receipt_chain"
    assert closure.value()["receipt_chain_sha256"] == page.digest
    obligation = authorization_obligation(page.value(), f"{r.event_seq:020}")
    assert obligation["status"] == "open" and obligation["terminal_reference"] is None


@pytest.mark.parametrize("corruption", ["read_at_ns", "committed_at_ns", "body"])
def test_invalid_newer_health_atomically_replaces_old_cache(corruption):
    from trusted_router.services.async_settle import Admission
    svc = service()
    svc.observer.clock = lambda: 10.
    svc.observer.install_workspace("ws", 10., Admission(0, 2))
    svc.observer.install_health(10., installed(health()))
    assert svc.observer.peek("ws")["prediction"] == "yes"
    bad = installed(health())
    bad[corruption] = None
    svc.observer.install_health(10., bad)
    assert svc.observer.peek("ws")["prediction"] == "unknown"
    assert svc.observer.health == bad


def test_future_host_claim_rejected_before_mutation_or_scan():
    svc = service()
    with pytest.raises(ValueError, match="a1-server-slot"):
        svc.store.claim(svc.journal.boot, NOW_US//2_000_000+100, NOW_US+200_000_000,
                        Budget(time.monotonic()+.5))
    assert svc.store.database.rows == {}
    assert svc.store.database.transaction_execute_sql_calls == 1
    assert svc.store.database.snapshot_execute_sql_calls == 0


def test_native_cycle_rpc_inventory(monkeypatch, shadow_deadline_clock, tmp_path):
    import json

    from google.cloud.spanner_v1.database import SnapshotCheckout
    from google.cloud.spanner_v1.types import CommitResponse, PartialResultSet
    from google.cloud.spanner_v1.types import Session as SessionPB

    from tests.test_async_settle_shadow_observation import sdk_database
    from trusted_router.storage_gcp_shadow_io import Sessions, observe_spanner_rpcs, rpc_scope
    db, clock, _ = sdk_database.__wrapped__(monkeypatch, shadow_deadline_clock)
    db.database_role = None
    db.sessions_manager = Sessions(db)
    db.snapshot = lambda: SnapshotCheckout(db)
    rows = {}
    calls = []
    def create(**kw):
        calls.append("session")
        return SessionPB(name=db.name+"/sessions/native")
    def query(**kw):
        calls.append("query")
        request = kw["request"]
        if "CURRENT_TIMESTAMP" in request.sql:
            fields = [("body", "STRING"), ("claim_at", "TIMESTAMP")]
            values = [None, "2026-10-12T00:00:00Z"]
        elif "unresolved_at" in request.sql:
            fields, values = [("unresolved_at", "TIMESTAMP"), ("actual_cost_micro", "INT64"), ("status", "STRING")], []
        else:
            fields = [("body", "STRING")]
            # Native Struct field access yields the bound Python string.
            key = (request.params["kind"], request.params["id"])
            values = [rows[key]] if key in rows else []
        response = PartialResultSet(metadata=dict(transaction=dict(id=b"offline",
            read_timestamp="2026-10-12T00:00:00.000000123Z"), row_type=dict(fields=[
                dict(name=n, type=dict(code=t)) for n, t in fields])))
        for value in values:
            if value is None:
                response._pb.values.add(null_value=0)
            else:
                response._pb.values.add(string_value=value)
        return iter([response])
    def commit(**kw):
        calls.append("commit")
        for mutation in kw["request"].mutations:
            for row in mutation.insert_or_update.values:
                rows[(row[0], row[1])] = row[2]
        return CommitResponse(commit_timestamp="2026-10-12T00:00:00.000000456Z")
    db.spanner_api.create_session, db.spanner_api.execute_streaming_sql, db.spanner_api.commit = create, query, commit
    def transaction(callback, **kwargs):
        session = db.sessions_manager.get_session(None)
        try:
            return session.run_in_transaction(callback, **kwargs)
        finally:
            db.sessions_manager.put_session(session)
    db.run_in_transaction = transaction
    store = ObservationStore(db, clock=clock.monotonic, wall=lambda: NOW_US/1e6)
    durations = []
    budget = Budget(clock.monotonic()+.5, clock.monotonic)
    with rpc_scope(budget.deadline, .5, clock.monotonic), observe_spanner_rpcs(durations.append):
        assert store.claim("issuer", NOW_US//2_000_000, NOW_US, budget)
        value = store.observe("issuer", "us-central1", "a"*40, NOW_US//2_000_000, budget)
        assert store.publish(value, budget)
    assert calls == ["session", "query", "commit", "query", "query", "query", "commit"]
    assert len(durations) == len(calls) == 7
    (tmp_path/"publisher-rpcs.json").write_text(json.dumps(dict(cold_cycle_rpc_count=len(calls),
        calls=calls, warm_cycle_rpc_count=len(calls)-1)))


def test_capture_prediction_retains_exact_basis_and_blocks_on_rate():
    from trusted_router.async_settle_shadow_v2_schema import sha256
    from trusted_router.services.async_settle import Admission
    svc = service()
    svc.store.clock = lambda: 10.
    svc.observer.clock = lambda: 10.
    svc.observer.health = installed(health())
    svc.observer.install_workspace("ws", 10., Admission(0, 2))
    svc.basis_tick = 10.
    for _ in range(10):
        result = svc.port.capture_prediction("ws", NOW_US)
        assert result["prediction"] == "yes"
        saved = svc.bases.schema.parse(svc.bases.queue[-1].body)["bases"]
        assert len(saved) == 1 and result["basis_sha256"] == sha256(saved[0])
        assert saved[0]["since_reply_ns"] == 0
    assert svc.port.capture_prediction("ws", NOW_US) is None
    assert svc.port.lost.is_set()


def test_observer_midnight_outcome_releases_owner_only_after_old_day_accounting():
    svc = service()
    observer = svc.observer
    stamp = [NOW_US/1e6+86399.9]
    observer.evidence_wall = lambda: stamp[0]
    observer.clock = lambda: 10.
    observer.health_read = lambda: (_ for _ in ()).throw(RuntimeError("read failed"))
    with observer.lock:
        observer._count("health_reads")
        stamp[0] += 1  # Count and owner use the same enqueue stamp across midnight.
        observer.inflight_ticks[None] = 10.
    observer._health(10.)
    assert not observer.inflight_ticks and not observer.owners
    daily, _, _ = observer.snapshot_daily_counts()
    assert daily["2026-10-12"]["health_reads"] == 1
    assert daily["2026-10-12"]["read_failures"] == 1
    assert daily["2026-10-12"]["max_consecutive_failures"] == 1
    assert daily["2026-10-13"].get("read_failures", 0) == 0


@pytest.mark.asyncio
async def test_observer_close_keeps_queued_failures_owned():
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    svc = service()
    observer = svc.observer
    observer.executor = ThreadPoolExecutor(max_workers=1)
    entered, release = threading.Event(), threading.Event()
    def read():
        entered.set()
        assert release.wait(2)
        raise RuntimeError("read failed")
    observer.health_read = read
    started = observer.clock()
    with observer.lock:
        observer._count("health_reads")
        observer.inflight_ticks[None] = started
    job = observer.executor.submit(observer._health, started)
    assert entered.wait(2)
    closing = asyncio.create_task(observer.close())
    await asyncio.sleep(.01)
    assert observer.inflight_ticks and not closing.done()
    release.set()
    await closing
    job.result()
    assert not observer.inflight_ticks and observer.counts["read_failures"] == 1
