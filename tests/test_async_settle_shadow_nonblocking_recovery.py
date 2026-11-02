"""Nonblocking refunds and explicitly bounded worker evidence completion."""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_async_settle_shadow import NOW, context
from tests.test_async_settle_shadow_cohort_runtime import installed, request
from tests.test_async_settle_shadow_release_accounting import BASE, CollisionGuard


def test_refund_returns_while_guard_held_and_next_admission_recovers_capacity():
    shell, _, _ = installed()
    real = shell.guard
    probe = CollisionGuard(real)
    shell.guard = probe
    try:
        assert shell.admit(128 * 1024) is None
        with ThreadPoolExecutor(max_workers=1) as pool:
            with real:
                future = pool.submit(shell.release, 128 * 1024)
                assert probe.collided.wait(2)
                # Evaluate while held, but always unlock before joining the
                # executor so the round-1 blocking implementation fails cleanly.
                returned = threading.Event()
                future.add_done_callback(lambda _: returned.set())
                nonblocking = returned.wait(.1)
                if nonblocking:
                    assert list(shell.refunds) == [128 * 1024]
                    # This admission, while the idle worker still cannot own
                    # the guard, must apply the entire refund before its check.
                    assert shell.admit(128 * 1024) is None
                    assert not shell.refunds
            future.result(timeout=2)
        assert nonblocking, 'refund waited on the request path'
        assert (shell.pending, shell.queued_bytes) == (1, 256 * 1024)
        shell.release(128 * 1024)
        assert (shell.pending, shell.queued_bytes) == (0, BASE)
    finally:
        shell.guard = real
        shell.close()
        shell.worker.join(2)


class PortCollision:
    """Force contention exactly at one Port operation, not at prediction capture."""
    def __init__(self, lock):
        self.lock = lock
        self.attempted = threading.Event()
        self.calls = []

    def acquire(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        self.attempted.set()
        return self.lock.acquire(*args, **kwargs)

    def release(self):
        self.lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()


@pytest.mark.parametrize('operation', ['finalize', 'fold_counts', 'complete'])
def test_worker_momentary_port_collision_completes_slot(monkeypatch, operation):
    monkeypatch.setattr('time.time_ns', lambda: NOW * 10**9)
    shell, service, _ = installed()
    ctx = context()
    held, unlock = threading.Event(), threading.Event()
    real = service.lock
    probe = PortCollision(real)
    owner = None
    original = getattr(service.port, operation)
    def holder():
        with real:
            held.set()
            assert unlock.wait(2)
    def wrapped(*args, **kwargs):
        nonlocal owner
        if owner is None:
            owner = threading.Thread(target=holder)
            owner.start()
            assert held.wait(2)
            service.lock = probe
        return original(*args, **kwargs)
    monkeypatch.setattr(service.port, operation, wrapped)
    try:
        cap, size = shell.capture_terminal(request(), ctx.body, ctx.authorization, 'settle')
        assert cap is not None
        assert shell.dispatch(shell.work, cap, size)
        assert probe.attempted.wait(2)
        # A real 20ms owner collision, comfortably inside the worker budget.
        assert not unlock.wait(.02)
        unlock.set()
        shell.join()
        owner.join(2)
        service.lock = real
        slot = service.journal.slot(cap.reservation)
        assert slot.body is not None and slot.completion_requested
        service.sync_fold_obligations()
        assert not service.port.fold_obligations
        service.flush(service.store.clock()+2)
        assert service.journal.days[cap.reservation.day].hwm == 1
        assert service.journal.days[cap.reservation.day].receipts == 1
        assert any(kwargs.get('timeout') == .25 for _, kwargs in probe.calls)
    finally:
        unlock.set()
        if owner is not None:
            owner.join(2)
        service.lock = real
        shell.close()
        shell.worker.join(2)


@pytest.mark.parametrize('worker', [False, True])
def test_port_wait_is_explicit_and_bounded(worker):
    from tests.test_async_settle_shadow_observation import event_for
    shell, service, _ = installed()
    real = service.lock
    probe = PortCollision(real)
    try:
        reservation = service.port.reserve(NOW * 1000000)
        event = event_for(reservation)
        service.lock = probe
        def call():
            started = time.perf_counter()
            result = (service.port.finalize(reservation, event, worker=True) if worker else
                      service.port.finalize(reservation, event))
            return result, time.perf_counter()-started
        with ThreadPoolExecutor(max_workers=1) as pool:
            with real:
                result, elapsed = pool.submit(call).result(timeout=1)
                assert result is None  # Still held: timeout never invents a receipt.
        assert elapsed < .75
        if worker:
            assert elapsed >= .2
            assert probe.calls == [((), {'timeout': .25})]
        else:
            assert elapsed < .1
            assert probe.calls == [((), {'blocking': False})]
        assert service.journal.slot(reservation).body is None
    finally:
        service.lock = real
        shell.close()
        shell.worker.join(2)


@pytest.mark.parametrize('replay', [False, True])
def test_unexposed_authorize_hands_finalization_to_worker(monkeypatch, replay):
    import copy

    from tests.test_async_settle_shadow_cohort import additions
    from tests.test_async_settle_ticket import endpoint
    from trusted_router.async_settle_shadow_v2_evidence import EVENT
    from trusted_router.schemas import GatewayAuthorizeRequest

    monkeypatch.setattr('time.time_ns', lambda: NOW * 10**9)
    shell, service, _ = installed()
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=ctx.authorization.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    req = request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2')])
    held, unlock = threading.Event(), threading.Event()
    real = service.lock
    probe = PortCollision(real)
    owner = None
    original = service.port.finalize
    results = []
    shell.startup_attempt_result = lambda reservation, result: results.append(result)
    def holder():
        with real:
            held.set()
            assert unlock.wait(3)
    def wrapped(*args, **kwargs):
        nonlocal owner
        if owner is None:
            owner = threading.Thread(target=holder)
            owner.start()
            assert held.wait(2)
            service.lock = probe
        return original(*args, **kwargs)
    try:
        if replay:
            shell.authorize_http(req, body, ctx.authorization, additions(), [endpoint()],
                typed=True, federated=False, replay=False)
            shell.join()
        monkeypatch.setattr(service.port, 'finalize', wrapped)
        values = additions()
        before = copy.deepcopy(values)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(shell.authorize_http, req, body, ctx.authorization, values, [endpoint()],
                typed=True, federated=False, replay=replay)
            assert held.wait(2)
            # The request completes while the owner still holds the exact lock.
            future.result(timeout=1)
            assert values == before and 'billing_shadow_binding' not in values
            slot = service.journal.pages[-1].slots[-1]
            assert slot.body is None
            unlock.set()
        shell.join()
        owner.join(2)
        service.lock = real
        assert slot.body is not None and slot.completion_requested
        event = EVENT.parse(slot.body)
        assert event['phase'] == ('replay' if replay else 'authorize')
        if not replay:
            assert event['eligibility'] == 'unknown'
            assert event['diagnostic']['reason'] == 'missing_context'
        assert results[-1] == 'failed'  # No retroactive native success or exposure.
        assert service.counters[slot.reservation.day]['authorize_attempts'] == 1 + replay
        service.sync_fold_obligations()
        assert not service.port.fold_obligations
        service.flush(service.store.clock()+2)
        day = service.journal.days[slot.reservation.day]
        assert day.hwm == day.receipts == 1 + replay
        assert not service.journal.pages
        assert values == before
        assert (shell.pending, shell.queued_bytes) == (0, BASE)
        assert all(kwargs in ({'blocking': False}, {'timeout': .25}) for _, kwargs in probe.calls)
    finally:
        unlock.set()
        if owner is not None:
            owner.join(2)
        service.lock = real
        shell.close()
        shell.worker.join(2)


@pytest.mark.parametrize('failure', ['invalid_event', 'sealing'])
def test_worker_authorize_recovery_cannot_bypass_receipt_validation(failure):
    from tests.test_async_settle_shadow_cohort import inputs
    from tests.test_async_settle_shadow_observation import event_for

    shell, service, _ = installed()
    try:
        value = inputs()
        value.reservation = service.port.reserve(NOW * 1000000)
        value.event = event_for(value.reservation)
        if failure == 'invalid_event':
            value.event['v'] = 99
        else:
            service.journal.days[value.reservation.day].sealing = True
        size = 4096
        assert shell.admit(size) is None
        assert shell.dispatch(shell.input_work, value, False, size)
        shell.join()
        slot = service.journal.slot(value.reservation)
        assert slot.body is None and not slot.completion_requested
        assert service.journal.days[value.reservation.day].hwm == 0
        assert service.port.lost.is_set() and not service.port.fold_obligations
        assert (shell.pending, shell.queued_bytes) == (0, BASE)
    finally:
        shell.close()
        shell.worker.join(2)
