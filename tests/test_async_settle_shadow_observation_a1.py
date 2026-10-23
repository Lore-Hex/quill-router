"""A1 review witnesses through the service and native SDK boundaries."""
from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    event_for,
    health,
    installed,
    logical_time,
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


def test_selected_v2_does_not_mutate_shared_adapter(tmp_path):
    import inspect
    import os
    import subprocess
    import sys
    script = "import pytest\nfrom pathlib import Path\n" + inspect.getsource(_prove_shared_adapter)
    script += "\n_prove_shared_adapter(Path("+repr(str(tmp_path))+"), pytest.MonkeyPatch())\n"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,  # noqa: S603
        env={**os.environ, "TR_STORAGE_BACKEND": "memory", "TR_ENVIRONMENT": "test"}, timeout=300)
    assert result.returncode == 0, result.stdout+result.stderr


def _prove_shared_adapter(tmp_path, monkeypatch):
    """Runtime before/after proof; no frozen version of the owner's adapter."""
    import contextvars
    import gc
    import hashlib
    import importlib
    import inspect
    import json
    import sys
    import tracemalloc
    from types import CodeType, SimpleNamespace

    from google.api_core.client_options import ClientOptions
    from google.auth.credentials import AnonymousCredentials
    from google.cloud.spanner_v1 import Client

    from trusted_router import storage_gcp_io as shared

    def code_hash(code):
        # marshal's reference/intern flags can change when unrelated imports
        # retain a constant. Hash the immutable code fields, not marshal flags.
        constants = tuple(code_hash(v) if isinstance(v, CodeType) else repr(v) for v in code.co_consts)
        fields = (code.co_code, constants, code.co_names, code.co_varnames, code.co_freevars,
            code.co_cellvars, code.co_argcount, code.co_kwonlyargcount, code.co_posonlyargcount,
            code.co_flags, code.co_exceptiontable, code.co_linetable, code.co_firstlineno, code.co_filename)
        return hashlib.sha256(repr(fields).encode()).hexdigest()

    def function_state(value, seen=None):
        seen = set() if seen is None else seen
        if id(value) in seen:
            return ("reference", id(value))
        seen.add(id(value))
        def state(member):
            return function_state(member, seen) if inspect.isfunction(member) else repr(member)
        return (id(value), id(value.__code__), code_hash(value.__code__),
            repr(value.__defaults__), repr(value.__kwdefaults__), repr(value.__annotations__),
            tuple((key, state(member)) for key, member in sorted(vars(value).items())),
            tuple(state(cell.cell_contents) for cell in value.__closure__ or ()))

    def snapshot():
        result = {"namespace_keys": tuple(sorted(vars(shared)))}
        context = contextvars.copy_context()
        for name, value in vars(shared).items():
            if name == "__builtins__":
                result[name] = id(value)
                continue
            code = function_state(value) if inspect.isfunction(value) else None
            state = (value in context, repr(value), repr(value.get(None))) if isinstance(value, contextvars.ContextVar) else repr(value)
            members = ()
            if inspect.isclass(value):
                members = tuple((key, function_state(member) if inspect.isfunction(member) else
                    function_state(member.__func__) if isinstance(member, (classmethod, staticmethod)) and inspect.isfunction(member.__func__) else repr(member))
                    for key, member in sorted(vars(value).items()))
            result[name] = (id(value), code, state, members)
        return result

    def money_scenario():
        calls = []
        def rpc(*args, **kwargs):
            calls.append((args, kwargs))
            return "response"
        db = SimpleNamespace(spanner_api=SimpleNamespace(commit=rpc, execute_sql=rpc),
            run_in_transaction=lambda fn, **kw: fn(kw))
        shared.configure_spanner_rpc_deadlines(db)
        gc.collect()
        tracemalloc.start()
        try:
            with shared.spanner_rpc_deadline(101.), shared.count_spanner_rpcs() as count:
                db.spanner_api.execute_sql("fixed select", retry=None)
                db.spanner_api.commit("fixed commit", retry=None)
                blocks = sorted((v.traceback[0].lineno, v.count, v.size)
                    for v in tracemalloc.take_snapshot().statistics("lineno")
                    if v.traceback[0].filename == shared.__file__)
                return calls, count.value(), blocks
        finally:
            tracemalloc.stop()

    from tests.fakes.spanner import make_fake_store
    from trusted_router.storage_gcp_counters import CREDIT_BALANCE_TABLE
    from trusted_router.storage_models import CreditAccount

    def money_store_scenario():
        store, database = make_fake_store()
        store._write_entity("credit", "ws_fixed", CreditAccount(workspace_id="ws_fixed"))
        database.typed.setdefault(CREDIT_BALANCE_TABLE, {})[("ws_fixed", 0)] = {
            "workspace_id": "ws_fixed", "shard": 0, "total_credits": 0,
            "total_usage": 0, "reserved": 0, "source_updated_at": None, "updated_at": None}
        gc.collect()
        tracemalloc.start()
        try:
            assert store.credit_workspace_once("ws_fixed", 5_000_000, "evt_fixed")
            assert not store.credit_workspace_once("ws_fixed", 5_000_000, "evt_fixed")
            balance = database.typed[CREDIT_BALANCE_TABLE][("ws_fixed", 0)]["total_credits"]
            assert balance == 5_000_000
            # Normalize interpreter method-lookup caches and collect cycles:
            # these are global runtime caches, not retained money-store state.
            # datetime.utcoffset creates a transient lookup-name cache entry.
            sys._clear_type_cache()
            gc.collect()
            blocks = sorted((v.traceback[0].filename.split("trusted_router/")[-1]+":"+str(v.traceback[0].lineno), v.count, v.size)
                for v in tracemalloc.take_snapshot().statistics("lineno")
                if "/trusted_router/" in v.traceback[0].filename)
            calls = (database.transaction_execute_sql_calls, database.snapshot_execute_sql_calls, database.commits)
            return balance, calls, blocks
        finally:
            tracemalloc.stop()

    monkeypatch.setattr(shared, "time", SimpleNamespace(monotonic=lambda: 100.))
    money_scenario()  # Warm the same code and ContextVar keys.
    money_store_scenario()
    before = snapshot()
    baseline = money_scenario()
    money_baseline = money_store_scenario()
    modules = sorted(path.stem for path in (Path.cwd()/"src/trusted_router").glob("*shadow*v2*.py"))
    modules += ["async_settle_shadow_journal", "async_settle_shadow_health_state",
        "async_settle_shadow_attachments", "storage_gcp_shadow_io", "storage_gcp_shadow_observation",
        "storage_gcp_shadow_attachments", "services.async_settle_shadow_health"]
    # No test helper or live v2 module is imported before the baseline.
    assert not any("shadow_v2" in name or "shadow_observation" in name for name in sys.modules)
    for name in modules:
        importlib.import_module("trusted_router."+name)
    client = Client(project="offline", credentials=AnonymousCredentials(),
                    client_options=ClientOptions(api_endpoint="localhost:1"), disable_builtin_metrics=True)
    money_db = client.instance("offline").database("offline")
    from trusted_router.storage_gcp_shadow_io import owned_database
    private = owned_database(money_db)
    assert private is not money_db and private.spanner_api is not money_db.spanner_api
    assert private.sessions_manager is not money_db.sessions_manager
    import google.auth

    from trusted_router.storage_gcp_shadow_attachments import GCSAttachments
    monkeypatch.setattr(google.auth, "default", lambda **kw: (AnonymousCredentials(), "offline"))
    transport = GCSAttachments("offline")
    assert transport.bucket == "offline"
    from tests.test_async_settle_shadow_observation import NOW_US, service
    from tests.test_async_settle_shadow_observation_a1 import MemoryAttachments
    svc = service()
    svc.bases.transport = svc.traces.transport = MemoryAttachments()
    svc.store.wall = lambda: NOW_US/1e6
    svc.publish_cycle(NOW_US//2_000_000, NOW_US)
    svc.store.wall = lambda: NOW_US/1e6+100
    svc.flush(svc.store.clock()+4)
    assert svc.store.database.commits
    after = snapshot()
    assert after == before, {k: (before.get(k), after.get(k)) for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    measured = money_scenario()
    assert measured[:2] == baseline[:2], (baseline, measured)
    assert [(line, count) for line, count, size in measured[2]] == [(line, count) for line, count, size in baseline[2]]
    # CPython key-sharing dict sizes can shrink with unrelated object creation;
    # preserve measured bytes as data, while asserting exact allocation counts.
    money_after = money_store_scenario()
    (tmp_path/"money-store-runtime.json").write_text(json.dumps(dict(logical_calls=money_after[1],
        retained_before=money_baseline[2], retained_after=money_after[2])))
    assert money_after[:2] == money_baseline[:2], (money_baseline[:2], money_after[:2])
    assert [(line, count) for line, count, size in money_after[2]] == [(line, count) for line, count, size in money_baseline[2]], (money_baseline[2], money_after[2])
    (tmp_path/"money-runtime.json").write_text(json.dumps(dict(rpc_count=baseline[1], retained_allocations_before=baseline[2], retained_allocations_after=measured[2])))
    money_db.spanner_api.transport.close()
    private.spanner_api.transport.close()


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
    svc.flush(svc.store.clock()+4)
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
        if args[2] is None:
            return real(*args)
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
    # The lifecycle lock is gone. Delay the worker in its executor, then verify
    # its I/O eligibility check uses actual entry time without touching State.
    from trusted_router.services.async_settle_shadow_health import publish_cycle
    svc = service()
    wall = [NOW_US]
    svc.store.wall = lambda: wall[0]/1e6
    calls = []
    monkeypatch.setattr(svc.store, "claim", lambda *a: calls.append(a))
    entered, release = threading.Event(), threading.Event()
    def block():
        entered.set()
        release.wait()
    blocker = svc.publisher_executor.submit(block)
    try:
        entered.wait()
        svc.schedule_cycle(NOW_US//2_000_000, NOW_US)
        job = svc.publisher_executor.submit(publish_cycle, svc.store, svc.journal.boot,
            svc.settings.effective_serving_region, svc.settings.source_revision,
            svc.state.job, NOW_US, svc)
        wall[0] += 300_000
    finally:
        release.set()
    blocker.result()
    svc.publisher_reply(job.result())
    assert calls == []
    counts = svc.counters["2026-10-12"]["publisher"]
    validate_publisher_final(counts)
    assert counts["missed_slots"] == 1


def test_midnight_queued_cycle_owns_day_before_executor_start():
    svc = service()
    wall = [NOW_US+86_398_000_000]
    svc.store.wall = lambda: wall[0]/1e6
    svc.observer.wall = svc.observer.evidence_wall = svc.store.wall
    svc.observer.clock = lambda: 0.
    svc.observer.daily_since = 0.
    release = threading.Event()
    blocker = svc.publisher_executor.submit(release.wait)
    stamp = wall[0]
    with svc.lock:
        svc.schedule_cycle(stamp//2_000_000, stamp)
        from trusted_router.services.async_settle_shadow_health import publish_cycle
        job = svc.publisher_executor.submit(publish_cycle, svc.store, svc.journal.boot,
            svc.settings.effective_serving_region, svc.settings.source_revision,
            svc.state.job, stamp, svc)
    wall[0] += 3_000_000
    try:
        svc.flush(svc.store.clock()+4)
        assert "2026-10-12" in svc.journal.days
    finally:
        release.set()
        svc.publisher_executor.shutdown(wait=True)
    blocker.result()
    svc.publisher_reply(job.result())
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
    def put(self, key, body, timeout, *, deadline=None, clock=None):
        self.calls.append(("put", key, len(body)))
        assert key not in self.objects
        self.objects[key] = body
        if self.lose:
            raise TimeoutError("lost")
    def get(self, key, timeout, *, deadline=None, clock=None):
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
    def delete(**kwargs):
        calls.append(kwargs)
        assert kwargs["retry"] is None
    db.spanner_api.delete_session = delete
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
    db.snapshot = lambda **options: SnapshotCheckout(db, **options)
    store = ObservationStore(db, clock=clock.monotonic, wall=lambda: NOW_US/1e6)
    cap = .25 if consumer == "health" else .5 if consumer == "observe" else 1.
    if checkout == "expired_session":
        db._sessions_manager = db.sessions_manager
        db.sessions_manager.session = db.session
    if checkout == "expired":
        clock.now += 2
    if checkout == "contended":
        class Contended:
            def acquire(self, *, timeout):
                assert 0 < timeout <= cap
                clock.now += timeout
                return False
            def release(self):
                pass
        db.sessions_manager.lock = Contended()
    budget = Budget(10.+cap, clock.monotonic)
    def read():
        if consumer == "observe":
            return store.observe("issuer", "us-central1", "a"*40, NOW_US//2_000_000, budget, claim_at_ns=NOW_US*1000)
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
            assert len(calls) == 4  # Cleanup precedes replacement on the later operation.
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
    page = svc.store.read(PAGE_KIND, f"{r.day}/{r.boot}/{r.first_seq:020}", Budget(logical_time()+1, logical_time))
    closure = svc.store.read(CLOSURE_KIND, r.day+"/"+r.boot, Budget(logical_time()+1, logical_time))
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
                        Budget(logical_time()+.5, logical_time))
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
    db.snapshot = lambda **options: SnapshotCheckout(db, **options)
    rows = {}
    calls = []
    rpc_times = []
    fail_commit = [False]
    def elapsed(microseconds):
        clock.now += microseconds/1_000_000
        rpc_times.append(microseconds)
    def create(**kw):
        calls.append("session")
        elapsed(2000)
        return SessionPB(name=db.name+"/sessions/native")
    def query(**kw):
        calls.append("query")
        elapsed(1000)
        request = kw["request"]
        if "CURRENT_TIMESTAMP" in request.sql:
            fields = [("body", "STRING"), ("claim_at", "TIMESTAMP")]
            values = [rows.get(("async_settle_shadow_health_control_v2", "local/publish")), "2026-10-12T00:00:00Z"]
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
        elapsed(10000)
        if fail_commit[0]:
            raise RuntimeError("lost commit reply")
        for mutation in kw["request"].mutations:
            for row in [*mutation.insert_or_update.values, *mutation.insert.values]:
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
        value = store.observe("issuer", "us-central1", "a"*40, NOW_US//2_000_000, budget, claim_at_ns=NOW_US*1000)
        assert value["observed_at_ns"] == NOW_US*1000+123
        assert store.publish(value, budget)
    assert calls == ["session", "query", "commit", "query", "query", "query", "commit"]
    assert len(durations) == len(calls) == 7
    cold = list(calls)
    calls.clear()
    rows.clear()
    budget = Budget(clock.monotonic()+.5, clock.monotonic)
    with rpc_scope(budget.deadline, .5, clock.monotonic), observe_spanner_rpcs(durations.append):
        assert store.claim("issuer", NOW_US//2_000_000, NOW_US, budget)
        value = store.observe("issuer", "us-central1", "a"*40, NOW_US//2_000_000, budget, claim_at_ns=NOW_US*1000)
        assert store.publish(value, budget)
    assert calls == ["query", "commit", "query", "query", "query", "commit"]
    (tmp_path/"publisher-rpcs.json").write_text(json.dumps(dict(cold_cycle_rpc_count=len(cold),
        calls=cold, warm_calls=calls, warm_cycle_rpc_count=len(calls))))

    from tests.test_async_settle_shadow_observation import maximum_rows
    from trusted_router.async_settle_shadow_journal import CLAIM_KIND, CLOSE_RESULT_KIND, Row
    from trusted_router.async_settle_shadow_v2_a1_health import validate_publisher_traces
    pages, counter, closure = maximum_rows()
    calls.clear()
    store.flush(pages, counter, closure, Budget(clock.monotonic()+4, clock.monotonic))
    assert calls == ["query"]*7+["commit"]
    from pathlib import Path
    result = dict(json.loads((Path(__file__).parent/"fixtures/async_settlement/shadow_v2_a1/close_result.json").read_text())["variants"][0]["object"], boot=counter.value()["boot"])
    calls.clear()
    store.close_result(Row.build(CLOSE_RESULT_KIND, counter.key, result), Budget(clock.monotonic()+4, clock.monotonic))
    assert calls == ["query", "query", "commit"]
    assert CLAIM_KIND == "async_settle_shadow_health_control_v2"
    for outcome in ("lost_claim", "published", "failed", "missed", "empty"):
        svc = service()
        svc.store = store
        if outcome != "lost_claim":
            rows.clear()
        fail_commit[0] = outcome == "failed"
        offset = len(rpc_times)
        if outcome == "missed":
            svc.missed_slot(NOW_US//2_000_000)
        elif outcome != "empty":
            svc.publish_cycle(NOW_US//2_000_000, NOW_US)
        entries = [entry for item in svc.traces.queue for entry in svc.traces.schema.parse(item.body)["entries"]]
        publisher = svc.counters["2026-10-12"]["publisher"]
        first = last = NOW_US//2_000_000 if entries else None
        validate_publisher_traces(publisher, entries, day="2026-10-12", boot=svc.journal.boot,
                                  first_slot=first, last_slot=last)
        assert not svc.journal.days["2026-10-12"].losses
        expected = rpc_times[offset:]
        if entries:
            trace = entries[0]
            assert trace["outcome"] == outcome
            assert trace["rpc_count"] == len(expected)
            assert trace["rpc_elapsed_us"] == sum(expected)
            assert trace["cycle_us"] == (sum(expected) if outcome != "missed" else None)
            if outcome == "lost_claim":
                assert trace["claim_at_ns"] is None
                assert trace["rpc_count"] == 1  # Committed claim snapshot, no transaction.
        histogram = publisher["rpc_us"]
        expected_counts = [0]*len(histogram["counts"])
        for elapsed_us in expected:
            expected_counts[sum(elapsed_us > edge for edge in histogram["bounds_us"])] += 1
        assert histogram["counts"] == expected_counts


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
    svc.loop = asyncio.get_running_loop()
    observer = svc.observer
    observer.executor = ThreadPoolExecutor(max_workers=1)
    entered, release = threading.Event(), threading.Event()
    def read():
        entered.set()
        release.wait()
        raise RuntimeError("read failed")
    observer.health_read = read
    started = observer.clock()
    with observer.lock:
        observer._count("health_reads")
        observer.inflight_ticks[None] = started
    job = observer.executor.submit(observer._health, started)
    entered.wait()
    closing = asyncio.create_task(observer.close())
    try:
        await asyncio.sleep(0)  # One explicit scheduler turn; no elapsed-time dependency.
        assert observer.inflight_ticks and not closing.done()
    finally:
        release.set()
        await closing
        observer.executor.shutdown(wait=True)
    await closing
    job.result()
    assert not observer.inflight_ticks and observer.counts["read_failures"] == 1


def test_missing_durable_page_blocks_journal_closure():
    """P3 verifies page durability; P5 independently validates exported coverage."""
    from trusted_router.async_settle_shadow_journal import PAGE_KIND
    svc = service()
    svc.journal.grant("2026-10-12", RESERVATION_BLOCK)
    r = svc.port.reserve(NOW_US)
    assert svc.port.finalize(r, event_for(r)) and svc.port.complete(r)
    svc.journal.freeze(r.day)
    with pytest.raises(ValueError, match="reference_missing"):
        svc.journal.verify_complete(r, [])
    assert svc.journal.days[r.day].hwm == 0
    assert not svc.journal.can_close(r.day, inflight=0, pending=0,
        interval_end_us=NOW_US, folded_us=NOW_US)
    assert not any(kind in (PAGE_KIND, CLOSURE_KIND) for kind, _ in svc.store.database.rows)


def test_reader_yes_cannot_erase_publisher_failure(monkeypatch):
    import copy

    from trusted_router.services.async_settle import Admission
    svc = service()
    svc.store.wall = lambda: NOW_US/1e6
    monkeypatch.setattr(svc.store, "claim", lambda *a: (_ for _ in ()).throw(UnknownCommit("lost")))
    svc.publish_cycle(NOW_US//2_000_000, NOW_US)
    before = copy.deepcopy(svc.counters["2026-10-12"]["publisher"])
    svc.observer.install_workspace("ws", svc.store.clock(), Admission(0, 2))
    svc.observer.install_health(svc.store.clock(), installed(health(), reply_at=svc.store.clock()))
    assert svc.observer.peek("ws")["prediction"] == "yes"
    svc.fold(NOW_US+100_000_000)
    assert svc.counters["2026-10-12"]["publisher"] == before
    assert before["failures"] == before["unknown_outcomes"] == 1


def test_literal_trace_is_per_cycle_not_cumulative(monkeypatch):
    svc = service()
    wall = [NOW_US]
    svc.store.wall = lambda: wall[0]/1e6
    def lose(*args):
        svc.store.last_claim = dict(claim_at_ns=wall[0]*1000)
        return False
    monkeypatch.setattr(svc.store, "claim", lose)
    for _ in range(2):
        svc.publish_cycle(wall[0]//2_000_000, wall[0])
        wall[0] += 2_000_000
        logical_time.advance(2)
    assert svc.traces.queue, "the cycle must retain a literal trace"
    entries = svc.traces.schema.parse(svc.traces.queue[0].body)["entries"]
    assert len(entries) == 2
    assert [e["outcome"] for e in entries] == ["lost_claim", "lost_claim"]
    assert [e["cycle_us"] for e in entries] == [0, 0]
    assert all(e["ack_at_us"] is None and e["observed_at_ns"] is None for e in entries)
    assert svc.counters["2026-10-12"]["publisher"]["lost_claims"] == 2


@pytest.mark.asyncio
async def test_close_result_separate_transaction_and_original_identity(monkeypatch):
    from trusted_router.async_settle_shadow_journal import COUNTER_KIND
    svc = service()
    rows = []
    real = svc.store.database.run_in_transaction
    def transaction(callback, **kwargs):
        before = dict(svc.store.database.rows)
        result = real(callback, **kwargs)
        rows.append({key[0] for key, value in svc.store.database.rows.items() if before.get(key) != value})
        return result
    monkeypatch.setattr(svc.store.database, "run_in_transaction", transaction)
    await svc.close()
    assert not svc.unclosed
    assert rows == [{COUNTER_KIND}, {COUNTER_KIND, CLOSURE_KIND}, {CLOSE_RESULT_KIND}]
    result = next(row for (kind, _), row in svc.store.database.rows.items() if kind == CLOSE_RESULT_KIND)
    import json
    value = json.loads(result.body)
    assert value["boot"] == svc.journal.boot and value["router_revision"] == "a"*40
    assert value["handoff_attempt_limit"] == 1
    assert value["flush_remaining_us"] <= 4_000_000
    assert value["shutdown_remaining_us"] <= 9_000_000


def test_health_read_preserves_O_and_snapshot_R():
    from trusted_router.async_settle_shadow_journal import HEALTH_KIND, Row
    svc = service()
    body = health()
    row = Row.build(HEALTH_KIND, "local/fleet", body)
    with svc.store.database.batch() as batch:
        svc.store.write(batch, [row], Budget(logical_time()+2, logical_time))
    svc.store.database.server_ns = NOW_US*1000+4_000_000_000
    read = svc.store.read_shadow_health()
    assert read["body"] == body
    assert read["read_at_ns"] == NOW_US*1000+4_000_000_000
    assert read["body"]["observed_at_ns"] == NOW_US*1000


def test_acknowledgement_is_observed_reply_not_C_or_R(monkeypatch):
    svc = service()
    wall = [NOW_US]
    svc.store.wall = lambda: wall[0]/1e6
    real = svc.store.publish
    def publish(value, budget):
        result = real(value, budget)
        logical_time.advance(.4)
        wall[0] += 400_000
        return result
    monkeypatch.setattr(svc.store, "publish", publish)
    monkeypatch.setattr(svc.store, "read_shadow_health", lambda: dict(
        committed_at_ns=NOW_US*1000+300_000_000, read_at_ns=NOW_US*1000+350_000_000))
    svc.publish_cycle(NOW_US//2_000_000, NOW_US)
    assert svc.traces.queue, "the cycle must retain a literal trace"
    entries = svc.traces.schema.parse(svc.traces.queue[0].body)["entries"]
    assert len(entries) == 1 and entries[0]["outcome"] == "published"
    assert entries[0]["ack_at_us"] == NOW_US+400_000
    assert entries[0]["cycle_us"] == 400_000


def test_observation_uses_unresolved_snapshot_not_scalar_clock(monkeypatch):
    svc = service()
    svc.store.database.server_ns = NOW_US*1000+123
    seen = []
    real = svc.store.snapshot_query
    def query(statement, budget, cap, **options):
        seen.append(statement[0])
        assert "tr_settle_outbox" in statement[0] and "CURRENT_TIMESTAMP" not in statement[0]
        return real(statement, budget, cap, **options)
    monkeypatch.setattr(svc.store, "snapshot_query", query)
    value = svc.store.observe("issuer", "us-central1", "a"*40,
        NOW_US//2_000_000, Budget(logical_time()+.5, logical_time), claim_at_ns=NOW_US*1000)
    assert len(seen) == 1 and "CURRENT_TIMESTAMP" not in seen[0]
    assert "tr_settle_outbox" in seen[0]
    assert value["observed_at_ns"] == NOW_US*1000+123
    assert value["sample_count"] == 0


def test_receipt_and_attachment_roots_are_full_digests():
    import hashlib

    from tests.test_async_settle_shadow_observation import journal
    j = journal()
    r = j.reserve(NOW_US)
    j.finalize(r, event_for(r))
    j.complete(r)
    pages = j.freeze(r.day)
    expected = hashlib.sha256(pages[0].body).hexdigest()
    j.acknowledge(pages)
    assert j.verify_complete(r, pages)
    assert j.days[r.day].chain == expected
    r2 = j.reserve(NOW_US+1)
    j.finalize(r2, event_for(r2))
    assert j.freeze(r2.day)[0].value()["previous_page_sha256"] == expected
    lane = Lane("basis", "issuer", MemoryAttachments())
    lane.append(r.day, basis())
    first = lane.queue[0].body
    lane.step(Budget(logical_time()+2, logical_time))
    assert lane.chains["2026-10-12"][1] == hashlib.sha256(first).hexdigest()
    lane.append(r.day, dict(basis(), since_reply_ns=1))
    assert lane.schema.parse(lane.queue[0].body)["previous_sha256"] == lane.chains["2026-10-12"][1]


def test_original_prediction_cannot_be_overwritten():
    import copy

    from tests.test_async_settle_shadow_observation import retained_case
    from trusted_router.async_settle_shadow_v2_evidence import EVENT
    svc, r, _ = retained_case("sample", pending=True)
    original = svc.journal.slot(r).body
    changed = EVENT.parse(original)
    assert changed["prediction"] is not None
    changed["prediction"] = copy.deepcopy(changed["prediction"])
    changed["prediction"]["prediction"] = "no" if changed["prediction"]["prediction"] == "yes" else "yes"
    with pytest.raises(ValueError, match="slot_conflict"):
        svc.journal.finalize(r, changed)
    assert svc.journal.slot(r).body == original


@pytest.mark.parametrize("stage", ["claim", "observe", "publish"])
@pytest.mark.parametrize("fault", ["late", "lost"])
def test_each_publisher_stage_uncertainty_is_failure(monkeypatch, stage, fault):
    svc = service()
    svc.store.wall = lambda: NOW_US/1e6
    real = getattr(svc.store, stage)
    def operation(*args, **kwargs):
        result = real(*args, **kwargs)
        if fault == "late":
            logical_time.advance(.500001)
            return result
        if stage == "observe":
            raise RuntimeError("snapshot response lost")
        raise UnknownCommit("commit response lost")
    monkeypatch.setattr(svc.store, stage, operation)
    svc.publish_cycle(NOW_US//2_000_000, NOW_US)
    counts = svc.counters["2026-10-12"]["publisher"]
    assert counts["publications"] == 0 and counts["failures"] == 1
    assert counts["late_cycles"] == int(fault == "late")
    assert counts["unknown_outcomes"] == int(fault == "lost" and stage != "observe")
    validate_publisher_final(counts)


def test_late_landed_close_result_requires_exact_reconciliation(monkeypatch):
    svc = service()
    svc.closing_reason = "shutdown"
    svc.termination_deadline = logical_time()+9
    database = svc.store.database
    real = database.run_in_transaction
    delayed = []
    def commit(*args, **kwargs):
        result = real(*args, **kwargs)
        if not delayed and any(kind == CLOSE_RESULT_KIND for kind, _ in database.rows):
            delayed.append(True)
            logical_time.advance(3.)  # Past result's 2 s child, within 4 s flush.
        return result
    monkeypatch.setattr(database, "run_in_transaction", commit)
    svc.flush(logical_time()+4)
    assert delayed == [True]
    assert svc.handoff is not None and svc.journal.days
    attempts = database.commits
    svc.flush(logical_time()+4)
    assert svc.handoff is None and not svc.journal.days
    assert database.commits == attempts  # Exact readback, never a second result write.
