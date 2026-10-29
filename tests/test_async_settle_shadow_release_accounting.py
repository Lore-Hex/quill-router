"""Charge conservation under real cross-thread guard contention."""
from __future__ import annotations

import random
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from tests.test_async_settle_shadow import NOW, context
from tests.test_async_settle_shadow_cohort import additions
from tests.test_async_settle_shadow_cohort_runtime import installed, request
from tests.test_async_settle_ticket import endpoint
from trusted_router.async_settle_shadow_v2_evidence import EVENT
from trusted_router.detached_jws import canonical
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services.async_settle_shadow_cohort_runtime import _CAPTURE

BASE = 32 * 4096


class CollisionGuard:
    """Observe a real failed acquisition before letting the holder unlock."""
    def __init__(self, lock):
        self.lock = lock
        self.collided = threading.Event()

    def acquire(self, blocking=True):
        acquired = self.lock.acquire(blocking=False)
        if acquired:
            return True
        self.collided.set()
        return self.lock.acquire() if blocking else False

    def release(self):
        self.lock.release()


def collide(shell, action):
    real = shell.guard
    probe = CollisionGuard(real)
    shell.guard = probe
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with real:
                future = pool.submit(action)
                assert probe.collided.wait(5), 'action never attempted the held guard'
            return future.result(timeout=5)
    finally:
        shell.guard = real


def test_collided_release_returns_exact_charge():
    shell, _, _ = installed()
    try:
        assert shell.admit(41327) is None
        collide(shell, lambda: shell.release(41327))
        shell.admit(256 * 1024 + 1)  # Reconcile published refunds without creating a charge.
        assert (shell.pending, shell.queued_bytes) == (0, BASE)
    finally:
        shell.close()
        shell.worker.join(5)


@pytest.mark.parametrize('seed', range(8))
def test_random_interleavings_conserve_admitted_charges(seed):
    rng = random.Random(seed)  # noqa: S311 - reproducible schedules
    shell, _, _ = installed()
    live = []
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for _ in range(40):
                # Batches race admission and refund, then check at a quiescent cut.
                with shell.guard:
                    shell.tokens = 10
                sizes = [rng.randrange(1, 50000) for _ in range(rng.randrange(1, 5))]
                releases = [live.pop(rng.randrange(len(live))) for _ in range(rng.randrange(len(live)+1))]
                operations = [('admit', size) for size in sizes] + [('release', size) for size in releases]
                rng.shuffle(operations)
                futures = [(kind, size, pool.submit(getattr(shell, kind), size)) for kind, size in operations]
                for kind, size, future in futures:
                    if future.result(timeout=5) is None and kind == 'admit':
                        live.append(size)
                if live:
                    size = live.pop(rng.randrange(len(live)))
                    collide(shell, lambda size=size: shell.release(size))
                shell.admit(256 * 1024 + 1)
                assert shell.pending == len(live)
                assert shell.queued_bytes == BASE + sum(live)
            for size in live:
                pool.submit(shell.release, size).result(timeout=5)
        shell.admit(256 * 1024 + 1)  # Reconcile published refunds without creating a charge.
        assert (shell.pending, shell.queued_bytes) == (0, BASE)
    finally:
        shell.close()
        shell.worker.join(5)


def test_collisions_cannot_starve_later_terminal_or_authorize(monkeypatch, record_property):
    monkeypatch.setattr('time.time_ns', lambda: NOW * 10**9)
    shell, service, _ = installed()
    ctx = context()
    # A valid small JSON snapshot with whitespace makes two charges leave fewer
    # bytes than either a fold or deferred authorize input. No bound is changed.
    raw = canonical(additions()['billing_snapshot']).decode()
    overhead = 2 * (12288 + 1) + 8192 + sum(map(len, ctx.authorization.candidate_endpoint_ids))
    length = (65500 - overhead) // 4
    ctx.authorization.pricing_snapshot = raw + ' ' * (length - len(raw))
    body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=ctx.authorization.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    captures = []
    bindings = []
    try:
        for _ in range(2):
            shell.tokens = 10
            cap, size = shell.capture_terminal(request(), ctx.body, ctx.authorization, 'settle')
            assert cap is not None
            # Real processing/folding completes before colliding its refund.
            shell.process(cap)
            collide(shell, lambda size=size: shell.release(size))
        leaked = (shell.pending, shell.queued_bytes)
        for _ in range(40):
            shell.tokens = 10
            shell.terminal(request(), ctx.body, ctx.authorization, 'settle',
                lambda: captures.append(_CAPTURE.get() is not None))
            shell.join()
            values = additions()
            shell.authorize_http(request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2')]),
                body, ctx.authorization, values, [endpoint()], typed=True, federated=False, replay=False)
            shell.join()
            bindings.append('billing_shadow_binding' in values)
            # Run the actual maintenance owner as requests continue.
            service.store.clock = lambda: 100. + len(bindings) * 6
            service.store.wall = lambda: NOW + len(bindings) * 6
            service.flush(service.store.clock() + 2)
        events = [EVENT.parse(slot.body) for page in service.journal.pages for slot in page.slots if slot.body]
        refusals = sum(e['diagnostic'] is not None and e['diagnostic']['reason'] == 'queue_full' for e in events)
        record_property('after_collisions', leaked)
        record_property('queue_full_receipts', refusals)
        record_property('later_bindings', sum(bindings))
        assert all(captures) and all(bindings), (leaked, captures, bindings, refusals)
        shell.admit(256 * 1024 + 1)  # Reconcile published refunds without creating a charge.
        assert (shell.pending, shell.queued_bytes) == (0, BASE)
    finally:
        shell.close()
        shell.worker.join(5)


def test_collided_fail_fold_is_consumed_by_owner():
    from tests.test_async_settle_shadow_observation import event_for
    shell, service, _ = installed()
    try:
        reservation = service.port.reserve(NOW * 1000000)
        assert reservation is not None
        assert service.port.finalize(reservation, event_for(reservation), fold_owed=True)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with service.lock:
                assert not pool.submit(service.port.fail_fold, reservation).result(timeout=5)
        service.sync_fold_obligations()
        assert reservation not in service.port.fold_obligations
        assert service.state.days[0].veto
        assert not service.journal.slot(reservation).completion_requested
    finally:
        shell.close()
        shell.worker.join(5)


@pytest.mark.filterwarnings('ignore::pytest.PytestUnhandledThreadExceptionWarning')
def test_dead_worker_returns_queued_charges_and_fails_fold(monkeypatch):
    monkeypatch.setattr('time.time_ns', lambda: NOW * 10**9)
    shell, service, _ = installed()
    entered, die = threading.Event(), threading.Event()
    def fatal():
        entered.set()
        assert die.wait(5)
        raise SystemExit('injected worker death')
    try:
        assert shell.dispatch(fatal)
        assert entered.wait(5)
        ctx = context()
        body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=ctx.authorization.model_id,
            route_type='chat.completions', stream=False, requested_parameters=[])
        shell.authorize_http(request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2')]),
            body, ctx.authorization, additions(), [endpoint()], typed=True, federated=False, replay=False)
        shell.terminal(request(), ctx.body, ctx.authorization, 'settle', lambda: None)
        from tests.test_async_settle_shadow_observation import event_for
        reservation = service.port.reserve(NOW * 1000000)
        event = event_for(reservation)
        assert service.port.finalize(reservation, event, fold_owed=True)
        shell.defer_fold(reservation, event, (), None)
        assert shell.pending == 3
        die.set()
        shell.worker.join(5)
        assert not shell.worker.is_alive()
        shell.admit(256 * 1024 + 1)  # Reconcile published refunds without creating a charge.
        assert (shell.pending, shell.queued_bytes) == (0, BASE)
        assert shell.queue.unfinished_tasks == 0
        assert list(service.port.fold_obligations.values()) == ['failed', 'failed']
        assert shell.admit(1) == 'queue_full'
    finally:
        die.set()
        shell.close()
        shell.worker.join(5)


@pytest.mark.parametrize('kind', ['terminal', 'fold', 'input'])
@pytest.mark.parametrize('failed', [False, True])
def test_worker_finally_refunds_under_contention(monkeypatch, kind, failed):
    from types import SimpleNamespace

    from tests.test_async_settle_shadow_observation import event_for
    monkeypatch.setattr('time.time_ns', lambda: NOW * 10**9)
    shell, service, _ = installed()
    try:
        if kind == 'terminal':
            ctx = context()
            cap, size = shell.capture_terminal(request(), ctx.body, ctx.authorization, 'settle')
            assert cap is not None
            def action():
                return shell.work(cap, size)
        else:
            reservation = service.port.reserve(NOW * 1000000)
            event = event_for(reservation)
            assert service.port.finalize(reservation, event, fold_owed=True)
            size = 4096
            assert shell.admit(size) is None
            if kind == 'fold':
                def action():
                    return shell.fold_work(reservation, event, (), None, size)
            else:
                inputs = SimpleNamespace(reservation=reservation, event=event, finalized=True)
                def action():
                    return shell.input_work(inputs, False, size)
        def broken(*args):
            raise ValueError('injected work failure')
        if failed:
            monkeypatch.setattr(shell, 'process' if kind == 'terminal' else 'fold_complete', broken)
        if failed and kind != 'terminal':
            with pytest.raises(ValueError, match='injected work failure'):
                collide(shell, action)
        else:
            collide(shell, action)
        shell.admit(256 * 1024 + 1)  # Reconcile published refunds without creating a charge.
        assert (shell.pending, shell.queued_bytes) == (0, BASE)
    finally:
        shell.close()
        shell.worker.join(5)


@pytest.mark.parametrize('operation', ['finalize', 'complete'])
def test_unfinished_evidence_recovery_seam(operation):
    """Contract witness: a contended Port call cannot invent a completed slot."""
    from tests.test_async_settle_shadow_observation import event_for
    shell, service, _ = installed()
    try:
        reservation = service.port.reserve(NOW * 1000000)
        event = event_for(reservation)
        if operation == 'complete':
            assert service.port.finalize(reservation, event)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with service.lock:
                args = (reservation, event) if operation == 'finalize' else (reservation,)
                assert not pool.submit(getattr(service.port, operation), *args).result(timeout=5)
        service.sync_fold_obligations()
        slot = service.journal.slot(reservation)
        assert not slot.completion_requested
        assert (slot.body is None) is (operation == 'finalize')
        assert service.journal.days[reservation.day].hwm == 0
        assert service.port.lost.is_set()
    finally:
        shell.close()
        shell.worker.join(5)
