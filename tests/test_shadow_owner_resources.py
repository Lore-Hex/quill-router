"""Real native lane ownership and real installed SIGTERM startup controls."""
import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from google.api_core.client_options import ClientOptions
from google.auth.credentials import AnonymousCredentials
from google.cloud.spanner_v1 import Client
from google.cloud.spanner_v1.session import Session

from tests.test_async_settle_shadow_expiry import pins
from tests.test_async_settle_shadow_observation import NOW_US, fake_store
from tests.test_async_settle_shadow_owner_stall import until
from trusted_router import async_settle_shadow_lifecycle as lifecycle
from trusted_router.services import async_settle_shadow_health as shell
from trusted_router.storage_gcp_shadow_io import rpc_scope
from trusted_router.storage_gcp_shadow_observation import ObservationStore


@pytest.fixture
def native_inventory(monkeypatch):
    live, deleted, closed = {}, [], []
    databases = []
    serial = 0
    faults = SimpleNamespace(delete=False)
    def create(session):
        nonlocal serial
        serial += 1
        session._session_id = str(serial)
        live[session.name] = dict(session.labels)
    def delete(session):
        if faults.delete:
            raise TimeoutError('offline cleanup unavailable')
        deleted.append(session.name)
        live.pop(session.name, None)
    monkeypatch.setattr(Session, 'create', create)
    monkeypatch.setattr(Session, 'delete', delete)
    base = Client(project='offline', credentials=AnonymousCredentials(),
        client_options=ClientOptions(api_endpoint='127.0.0.1:1'),
        disable_builtin_metrics=True).instance('i').database('d')
    store = ObservationStore(base, wall=lambda: NOW_US/1e6)
    databases.extend((base, store.database, store.workspace_database))
    original = ObservationStore.publisher_lane
    def lane(adapter):
        result = original(adapter)
        databases.append(result.database)
        transport = result.database.spanner_api.transport
        close = transport.close
        def tracked_close():
            closed.append(result.database)
            close()
        monkeypatch.setattr(transport, 'close', tracked_close)
        return result
    monkeypatch.setattr(ObservationStore, 'publisher_lane', lane)
    yield SimpleNamespace(store=store, live=live, deleted=deleted, closed=closed, faults=faults)
    for database in databases:
        database.spanner_api.transport.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('cleanup_failure', [False, True], ids=['clean', 'cleanup_retry'])
async def test_repeated_hangs_bound_native_sessions_and_resume(native_inventory, monkeypatch, cleanup_failure):
    inventory = native_inventory
    svc = shell.Service(pins(), inventory.store)
    svc.supervised = True
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    release = threading.Event()
    cleanup_waiting, cleanup_release = threading.Event(), threading.Event()
    def cleanup_sleep(seconds):
        assert seconds == 60
        cleanup_waiting.set()
        cleanup_release.wait()
    monkeypatch.setattr(shell, 'sleep', cleanup_sleep)
    executors = []
    budget = shell.PUBLISHER_ABANDONED_BUDGET
    assert budget == 2
    try:
        assert svc.store.prepare_session(svc.journal.boot)
        assert len(inventory.live) == 2
        svc.replace_publisher()
        for _ in range(budget):
            executors.append(svc.publisher_executor)
            assert await asyncio.wrap_future(svc.publisher_session_job)
            manager = svc.publisher_store.database._sessions_manager
            entered = threading.Event()
            def hang(manager=manager, entered=entered):
                with rpc_scope(time.monotonic()+5, 5, time.monotonic):
                    session = manager.get_session(None)
                    try:
                        entered.set()
                        release.wait()  # No deadline cooperation; one permanent native call.
                    finally:
                        manager.put_session(session)
            svc.publisher_worker_active.set()
            svc.publisher_worker = svc.publisher_executor.submit(hang)
            await until(entered.is_set)
            svc.replace_publisher()
        for _ in range(30):  # N >> R; further attempts cannot allocate or enqueue work.
            svc.replace_publisher()
            assert len(inventory.live) <= 3+budget
            assert sum(thread.is_alive() for ex in set(executors) for thread in ex._threads) <= budget+1
            assert not svc.contender()
            stamp = int(svc.store.wall()*1e6)
            assert not any(isinstance(e, lifecycle.StartPublisherCycle) for e in svc.tick(stamp))
        diagnostics = next(iter(svc.counters.values()))['diagnostics']
        assert diagnostics['publisher_abandoned_outstanding'] == budget
        assert diagnostics['publisher_budget_exhausted'] == 1
        assert not svc.stopped.is_set()
        inventory.faults.delete = cleanup_failure
        release.set()
        if cleanup_failure:
            await until(cleanup_waiting.is_set)
            svc.publisher_inventory()
            assert len(svc.abandoned_publishers) == budget and not svc.contender()
            assert len(inventory.live) <= 3+budget
            inventory.faults.delete = False
        cleanup_release.set()
        def reaped():
            svc.publisher_inventory()
            return not svc.abandoned_publishers
        await until(reaped)
        assert len(inventory.deleted) >= budget
        assert len(inventory.closed) >= budget
        assert diagnostics['publisher_abandoned_outstanding'] == 0
        assert diagnostics['publisher_budget_exhausted'] == 0
        assert svc.contender()
        svc.replace_publisher()
        assert await asyncio.wrap_future(svc.publisher_session_job)
        assert len(inventory.live) == 3
        assert any(isinstance(e, lifecycle.StartPublisherCycle) for e in svc.tick(stamp+2_000_000))
    finally:
        inventory.faults.delete = False
        release.set()
        cleanup_release.set()
        svc.retire_publisher()
        for job in svc.abandoned_publishers:
            await asyncio.wrap_future(job)
        for executor in (*executors, svc.publisher_executor, svc.session_executor, svc.evidence_executor):
            executor.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('reason', ['region', 'ineligible'])
async def test_reader_only_boot_allocates_two_sessions(native_inventory, monkeypatch, reason):
    svc = shell.Service(pins(), native_inventory.store)
    if reason == 'region':
        svc.settings.async_settle_shadow_health_region = 'other-region'
    else:
        svc.settings.async_settle_shadow_health_region = ''
    monkeypatch.setattr(svc, 'trap_sigterm', lambda: None)
    def poll():
        svc.runtime_stop.available = True
        return True
    monkeypatch.setattr(svc.runtime_stop, 'poll', poll)
    monkeypatch.setattr(svc.runtime_stop, 'start', lambda *args: None)
    monkeypatch.setattr(svc.observer, 'start', lambda: None)
    svc.next_flush = float('inf')
    svc.start()
    try:
        await until(lambda: svc.owner_task is not None)
        await asyncio.sleep(.03)
        assert len(native_inventory.live) == 2
        assert svc.publisher_lane_generation == 0
        assert svc.publisher_session_job is None
    finally:
        svc.stopped.set()
        for task in (svc.task, svc.owner_task):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for executor in (svc.publisher_executor, svc.session_executor, svc.evidence_executor):
            executor.shutdown(wait=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('phase', ['long_retry', 'blocked_preparation', 'blocked_preparation_retried'])
async def test_sigterm_interrupts_startup_and_preserves_final_flush(monkeypatch, phase):
    store, _ = fake_store()
    store.clock = time.monotonic
    svc = shell.Service(pins(), store)
    entered, release = threading.Event(), threading.Event()
    sleeping = asyncio.Event()
    delays = []
    attempts = []
    if phase == 'blocked_preparation_retried':
        monkeypatch.setattr(shell, 'START_ATTEMPT_SECONDS', .01)
    def prepare(*args):
        attempts.append(1)
        entered.set()
        if phase != 'long_retry':
            release.wait()
        return False
    monkeypatch.setattr(store, 'prepare_session', prepare)
    pause = svc.retry_pause
    async def accelerated_pause(delay):
        delays.append(delay)
        if delay < 16:
            await asyncio.sleep(0)
        else:
            sleeping.set()
            await pause(delay)
    monkeypatch.setattr(svc, 'retry_pause', accelerated_pause)
    previous = Mock()
    monkeypatch.setattr(shell.signal, 'getsignal', lambda _: previous)
    monkeypatch.setattr(shell.signal, 'signal', Mock())
    observer_close = AsyncMock()
    monkeypatch.setattr(svc.observer, 'close', observer_close)
    flushed = []
    def final_flush(deadline):
        flushed.append(deadline)
        yield from ()
    monkeypatch.setattr(svc, '_flush', final_flush)
    svc.start()
    try:
        if phase in ('long_retry', 'blocked_preparation_retried'):
            await asyncio.wait_for(sleeping.wait(), 2)
            assert delays == [1, 2, 4, 8, 16]
        else:
            await until(entered.is_set)
        if phase == 'blocked_preparation_retried':
            assert len(attempts) == 1
            assert len(svc.startup_job._done_callbacks) == 1  # One async bridge across five timeouts.
        start = time.monotonic()
        svc.signal_handler(shell.signal.SIGTERM, None)
        await until(lambda: svc.close_task is not None)
        await asyncio.wait_for(asyncio.shield(svc.close_task), 1)
        elapsed = time.monotonic()-start
        assert elapsed < 1  # Well inside the real nine-second SIGTERM grace.
        assert svc.task.done() and svc.close_finished
        observer_close.assert_awaited_once()
        previous.assert_called_once()
        assert flushed
        diagnostics = next(iter(svc.counters.values()))['diagnostics']
        assert diagnostics['startup_worker_outstanding'] == int(phase != 'long_retry')
        assert not release.is_set()  # close never joins the blocked native call.
    finally:
        release.set()
        svc.task.cancel()
        await asyncio.gather(svc.task, return_exceptions=True)
        for executor in (svc.publisher_executor, svc.session_executor, svc.evidence_executor):
            executor.shutdown(wait=True)
