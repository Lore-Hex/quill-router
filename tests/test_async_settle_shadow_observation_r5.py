"""Round-five production regressions; event/state proofs use controlled clocks."""
from __future__ import annotations

import copy
import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_observation import (
    NOW_US,
    deterministic_scheduler,  # noqa: F401
    health,
    installed,
    service,
)
from tests.test_async_settle_shadow_observation_a1 import MemoryAttachments, basis
from tests.test_async_settle_shadow_v2_evidence import exhaustive, untraced  # noqa: F401
from trusted_router.async_settle_shadow_attachments import Lane
from trusted_router.async_settle_shadow_journal import RESERVATION_BLOCK
from trusted_router.async_settle_shadow_v2_evidence import MAX_DAY_BUCKETS
from trusted_router.services.async_settle import Admission
from trusted_router.storage_gcp_shadow_observation import Budget


@pytest.mark.parametrize("landed", [False, True])
def test_basis_chain_returns_to_open_old_day(landed):
    transport = MemoryAttachments()
    lane = Lane("basis", "issuer", transport)
    old, new = "2026-10-12", "2026-10-13"
    lane.append(old, basis())
    first = lane.queue[0]
    if landed:
        lane.step(Budget(4., lambda: 0.))
        assert not lane.queue
    lane.append(new, basis())
    changed = basis()
    changed["read_elapsed_ns"] += 1
    lane.append(old, changed)
    last = lane.queue[-1]
    assert first.key != last.key and first.day == last.day == old
    assert lane.schema.parse(last.body)["sequence"] == "00000000000000000002"
    assert lane.schema.parse(last.body)["previous_sha256"] == hashlib.sha256(first.body).hexdigest()
    assert lane.schema.parse(next(item.body for item in lane.queue if item.day == new))["previous_sha256"] is None
    assert len(lane.queue) <= 4 and lane.retained_bytes <= 256*1024
    with pytest.raises(ValueError, match="attachment_pending"):
        lane.retire(old)
    clock = 5.
    while lane.queue:
        lane.step(Budget(clock+4, lambda clock=clock: clock))
        clock += 5
    assert old in lane.chains  # Upload does not retire metadata.
    lane.retire(old)
    assert old not in lane.chains
    with pytest.raises(ValueError, match="closed_day"):
        lane.append(old, basis())


def test_basis_chain_day_bound_survives_empty_queue():
    lane = Lane("basis", "issuer", MemoryAttachments())
    for offset in range(MAX_DAY_BUCKETS):
        lane.append(f"2026-10-{12+offset}", basis())
        lane.step(Budget(offset*5.+4, lambda offset=offset: offset*5.))
    assert not lane.queue and len(lane.chains) == MAX_DAY_BUCKETS
    with pytest.raises(ValueError, match="day_buckets"):
        lane.append("2026-10-20", basis())


class HeldLock:
    """A held lock fails any attempted blocking acquisition, without a timeout."""
    def __init__(self, delegate):
        self.delegate = delegate
        self.calls = []

    def acquire(self, blocking=True, timeout=-1):
        self.calls.append((blocking, timeout))
        assert blocking is False and timeout == -1, "request tried to wait"
        return self.delegate.acquire(blocking=False)

    def release(self):
        raise AssertionError("released an unacquired lock")

    def __enter__(self):
        self.acquire()

    def __exit__(self, *args):
        self.release()


@pytest.mark.parametrize("sealing", [False, True])
def test_every_port_method_with_each_owned_lock_held(monkeypatch, sealing):
    svc = service()
    svc.observer.health = installed(health(), reply_at=svc.store.clock())
    svc.observer.install_workspace("ws", svc.store.clock(), Admission(0, 2))
    day = "2026-10-12"
    svc.journal.grant(day, RESERVATION_BLOCK)
    from tests.test_async_settle_shadow_observation import fixture
    from trusted_router.async_settle_shadow_v2_evidence import COUNTS
    original = svc.port.reserve(NOW_US)
    event = copy.deepcopy(next(v["object"] for v in fixture("receipt_pages")
                               if v["schema"] == "TERMINAL_EVENT" and v["expected"] == "accept"))
    event.update(seq=f"{original.event_seq:020}", event_at_us=original.observed_us,
                 receipt_at_us=original.observed_us, partition="C", eligibility="unknown",
                 proof_kind="none", original_receipt=None, certificate_sha256=None,
                 booking=dict(outcome="pending", reference=None, winner="settle"))
    assert svc.port.finalize(original, event)
    original_page = svc.journal.freeze(day)[0]
    reservation = svc.port.reserve(NOW_US+1)
    event.update(seq=f"{reservation.event_seq:020}", event_at_us=reservation.observed_us,
                 receipt_at_us=reservation.observed_us, phase="completion", completion_of=f"{original.event_seq:020}",
                 booking=dict(outcome="settled", reference="0"*64, winner="settle"))
    bucket = dict(phase="settle", dimensions=dict(adapter="openai", route_type="chat.completions", streamed=False),
                  **dict.fromkeys(COUNTS, 0), eligible_demand=1)
    bucket.update(settle_attempts=1, C=1, requested_eligible=1)
    # All entry points have valid inputs; no early schema rejection can
    # hide a deeper acquisition. Complete_event reaches contextual validation.
    assert svc.port.complete_event(reservation, original_page, event)
    methods = {
        "call": lambda: svc.port.call(lambda: True),
        "record_startup_unprovisioned": lambda: svc.port.record_startup_unprovisioned(NOW_US),
        "reserve": lambda: svc.port.reserve(NOW_US),
        "finalize": lambda: svc.port.finalize(reservation, event, fold_owed=True),
        "fail_fold": lambda: svc.port.fail_fold(reservation),
        "complete": lambda: svc.port.complete(reservation),
        "complete_event": lambda: svc.port.complete_event(reservation, original_page, event),
        "fold_counts": lambda: svc.port.fold_counts(day, bucket),
        "capture_prediction": lambda: svc.port.capture_prediction("ws", NOW_US),
    }
    svc.journal.days[day].sealing = sealing
    import inspect
    assert {name for name, _ in inspect.getmembers(type(svc.port), inspect.isfunction)
            if not name.startswith("_")} == set(methods)
    objects = [svc, svc.observer, svc.bases, svc.traces, svc.port.lost]
    locks = [(obj, name) for obj in objects for name, value in vars(obj).items()
             if callable(getattr(value, "acquire", None)) and callable(getattr(value, "release", None))]
    assert {(type(obj).__name__, name) for obj, name in locks} == {
        ("Service", "lock"), ("Service", "admission_lock"),
        ("HealthObserver", "lock"), ("Lane", "lock")}
    from contextlib import contextmanager
    with ThreadPoolExecutor(max_workers=1) as worker:
        @contextmanager
        def held(obj, name):
            original_lock = getattr(obj, name)
            entered, release = threading.Event(), threading.Event()
            def own():
                with original_lock:
                    entered.set()
                    release.wait()
            future = worker.submit(own)
            try:
                entered.wait()
                gate = HeldLock(original_lock)
                with monkeypatch.context() as patch:
                    patch.setattr(obj, name, gate)
                    yield gate
            finally:
                release.set()
                future.result()
        for obj, name in locks:
            with held(obj, name) as gate:
                for method, call in methods.items():
                    gate.calls.clear()
                    svc.port.lost.value = False  # Independent fault trials only.
                    svc.basis_tokens = 10
                    result = call()
                    if gate.calls:
                        assert result is None or result is False, method
                        assert svc.port.lost.is_set(), method
        # The actual RLock is owned by the worker, exactly as in the reviewer
        # witness; an attempted blocking call fails before it could hang CI.
        svc.journal.days[day].sealing = False
        with held(svc.bases, "lock") as gate:
            svc.basis_tokens = 10
            assert svc.port.capture_prediction("ws", NOW_US) is None
            assert gate.calls == [(False, -1)] and svc.port.lost.is_set()


def test_request_reserve_worker_finalize_once_blocks_freeze_and_close():
    svc = service()
    day = "2026-10-12"
    svc.journal.grant(day, RESERVATION_BLOCK)
    svc.journal.terminal_permits[day] = RESERVATION_BLOCK
    reservation = svc.port.reserve(NOW_US, terminal=True)
    assert reservation is not None
    assert svc.journal.freeze(day) == []
    assert svc.journal.days[day].hwm == 0
    svc.journal.days[day].closing = True
    assert not svc.journal.can_close(day, inflight=0, pending=0, interval_end_us=NOW_US, folded_us=NOW_US)
    from tests.test_async_settle_shadow_observation import fixture
    from trusted_router.async_settle_shadow_v2_evidence import EVENT
    event = copy.deepcopy(next(v["object"] for v in fixture("receipt_pages")
                               if v["schema"] == "TERMINAL_EVENT" and v["expected"] == "accept"))
    event.update(seq=f"{reservation.event_seq:020}", event_at_us=reservation.observed_us,
                 receipt_at_us=reservation.observed_us, partition="C", eligibility="unknown",
                 proof_kind="none", original_receipt=None, certificate_sha256=None)
    EVENT(event)
    origin = threading.get_ident()
    def finalize():
        assert threading.get_ident() != origin
        return svc.port.finalize(reservation, event)
    with ThreadPoolExecutor(max_workers=1) as worker:
        assert worker.submit(finalize).result()
    original = svc.journal.slot(reservation).body
    changed = copy.deepcopy(event)
    changed["eligibility"] = "excluded"
    EVENT(changed)
    assert svc.port.finalize(reservation, changed) is None
    assert svc.journal.slot(reservation).body == original
    assert svc.journal.days[day].hwm == 0  # Finalization is not durability.
    assert svc.port.complete(reservation)
    svc.flush(svc.store.clock()+4)
    assert svc.journal.days[day].hwm == reservation.event_seq


def test_owned_client_never_initializes_global_metrics(monkeypatch):
    from google.auth.credentials import Credentials
    from google.cloud.spanner_v1 import Client
    from google.cloud.spanner_v1 import client as sdk
    from opentelemetry import metrics

    from trusted_router.storage_gcp_shadow_io import owned_database
    class Static(Credentials):
        def refresh(self, request):
            raise AssertionError("no network")
    monkeypatch.delenv("SPANNER_EMULATOR_HOST", raising=False)
    monkeypatch.delenv("SPANNER_DISABLE_BUILTIN_METRICS", raising=False)
    calls = []
    monkeypatch.setattr(sdk, "_initialize_metrics", lambda *a, **kw: calls.append((a, kw)))
    before = metrics.get_meter_provider()
    source = Client(project="offline", credentials=Static(), disable_builtin_metrics=True).instance("offline").database("offline")
    owned = owned_database(source)
    assert owned is not source
    assert calls == [] and metrics.get_meter_provider() is before


@exhaustive
def test_encoding_failure_always_stops_tracemalloc(monkeypatch, tmp_path):
    import tracemalloc

    from tests.test_async_settle_shadow_observation import test_native_encoding_six_maximum_rows
    from trusted_router.storage_gcp_shadow_observation import ObservationStore
    def fail(*args):
        raise TimeoutError("injected encoding deadline")
    monkeypatch.setattr(ObservationStore, "write", fail)
    assert not tracemalloc.is_tracing()
    try:
        with pytest.raises(TimeoutError, match="injected encoding deadline"):
            test_native_encoding_six_maximum_rows(tmp_path)
        assert not tracemalloc.is_tracing()
    finally:
        tracemalloc.stop()  # The negative-control mutant cannot pollute its worker.


def test_attachment_admission_caps_absolute_shutdown_deadline():
    svc = service()
    now = svc.store.clock()
    svc.termination_deadline = now + .25
    calls = []
    class Transport:
        def put(self, key, body, timeout, *, deadline, clock):
            calls.append((timeout, deadline))
    svc.bases.transport = Transport()
    svc.bases.append("2026-10-12", dict(basis(), reader_boot=svc.journal.boot))
    svc.flush(now + 4.)
    assert calls == [(.25, now + .25)]


@pytest.mark.parametrize("landed", [False, True])
def test_port_basis_interleaves_owned_days(landed):
    svc = service()
    mono = [10.]
    svc.store.clock = svc.observer.clock = lambda: mono[0]
    svc.basis_tick = 10.
    svc.bases.transport = MemoryAttachments()
    old_stamp = NOW_US + 86_399_900_000
    svc.journal.grant("2026-10-12", RESERVATION_BLOCK)
    pending = svc.port.reserve(old_stamp)
    assert pending is not None
    svc.observer.health = installed(health(now_us=NOW_US+86_398_000_000))
    svc.observer.install_workspace("ws", 10., Admission(0, 2))
    assert svc.port.capture_prediction("ws", old_stamp)
    first = svc.bases.queue[0]
    if landed:
        svc.bases.step(Budget(14., lambda: mono[0]))
        assert not svc.bases.queue
    svc.ensure_day(NOW_US+86_400_100_000)
    mono[0] = 10.2
    assert svc.port.capture_prediction("ws", NOW_US+86_400_100_000)
    mono[0] = 10.3
    assert svc.port.capture_prediction("ws", pending.observed_us)
    last = svc.bases.queue[-1]
    assert first.key != last.key
    assert svc.bases.schema.parse(last.body)["previous_sha256"] == hashlib.sha256(first.body).hexdigest()
    assert not svc.port.lost.is_set() and svc.journal.days[pending.day].hwm == 0


def test_basis_metadata_retires_only_with_durable_close_result(monkeypatch):
    from trusted_router.storage_gcp_shadow_observation import UnknownCommit
    svc = service()
    day = "2026-10-12"
    svc.bases.transport = MemoryAttachments()
    svc.bases.append(day, dict(basis(), reader_boot=svc.journal.boot))
    svc.closing_reason = "shutdown"
    svc.termination_deadline = svc.store.clock()+9
    svc.interval_end_us = int(svc.store.wall()*1_000_000)
    original = svc.store.close_result
    def lost(*args):
        raise UnknownCommit("unobserved result")
    monkeypatch.setattr(svc.store, "close_result", lost)
    svc.flush(svc.store.clock()+4)
    assert not svc.bases.queue and day in svc.bases.chains
    assert svc.handoff is not None and day in svc.journal.days
    result = svc.handoff[-1]
    original(result, Budget(svc.store.clock()+2, svc.store.clock))
    svc.flush(svc.store.clock()+4)
    assert day not in svc.bases.chains and day not in svc.journal.days


def test_p3_sql_dispatch_statically_resolves_every_statement():
    import ast
    from pathlib import Path

    from tests import test_spanner_googlesql_shape as guard
    from trusted_router import storage_gcp_shadow_observation as native
    tree = ast.parse(Path(native.__file__).read_text())
    bound = guard._bound_strings(tree)
    variants = set()
    sites = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or guard._call_name(node) not in guard.SQL_METHODS:
            continue
        sites += 1
        argument = node.args[0] if node.args else guard._keyword(node, "sql")
        resolved = guard._fold(argument, bound) if argument is not None else []
        assert resolved and all(guard.PLACEHOLDER not in sql for sql in resolved), node.lineno
        variants.update(resolved)
    assert sites == 2 and variants == {native.POINT_SQL, native.CLAIM_SQL, native.HEALTH_SQL, native.UNRESOLVED_SQL}
