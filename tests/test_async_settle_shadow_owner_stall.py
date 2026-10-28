"""Current-source negative controls: real owner, adapter, transaction and session lane."""
from __future__ import annotations

import asyncio
import copy
import inspect
import json
import textwrap
import threading
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.shadow_observation_hygiene import package_hygiene  # noqa: F401
from tests.test_async_settle_shadow_expiry import pins
from tests.test_async_settle_shadow_health_watchdog import accounting
from tests.test_async_settle_shadow_observation import NOW_US, fake_store
from trusted_router.async_settle_shadow_journal import CLAIM_KIND, COUNTER_KIND, HEALTH_KIND
from trusted_router.async_settle_shadow_stop import RuntimeStop
from trusted_router.services import async_settle_shadow_health as shell
from trusted_router.storage_gcp_shadow_io import Sessions, configure


async def until(predicate, seconds=2.):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.005)


class Attachments:
    def __init__(self):
        self.objects = {}

    def put(self, key, body, timeout, *, deadline, clock):
        assert 0 < timeout <= 2 and clock() < deadline
        assert key not in self.objects or self.objects[key] == body
        self.objects[key] = body

    def get(self, key, timeout, *, deadline, clock):
        return self.objects.get(key)


class NativeLane:
    """SDK-shaped offline transport: real Sessions checkout and bounded commit wrapper."""
    def __init__(self, db, at, fault, lane):
        self.db, self.at, self.fault, self.lane = db, at, fault, lane
        self._sessions_manager = Sessions(self, lane=lane)
        self._sessions_manager.session = SimpleNamespace(delete=lambda: None)
        self.spanner_api = SimpleNamespace(commit=self.commit)
        configure(self)

    def commit(self, txn, **kwargs):
        if (self.lane == 'publisher' and not self.fault.used
                and any(row[2] == self.fault.kind for row in txn.pending_writes)):
            self.fault.used = True
            self.fault.entered.set()
            if self.fault.mode == 'lands':
                assert self.fault.commit(txn)
            self.fault.release.wait()  # Deliberately ignores the native RPC deadline.
            raise TimeoutError('offline lost acknowledgement')
        return self.fault.commit(txn)

    @contextmanager
    def snapshot(self, **kwargs):
        manager = self._sessions_manager
        session = manager.get_session(None)
        try:
            with self.db.snapshot(**kwargs) as snapshot:
                yield snapshot
        finally:
            manager.put_session(session)

    def run_in_transaction(self, callback, **kwargs):
        manager = self._sessions_manager
        session = manager.get_session(None)
        self.fault.local.lane = self
        try:
            return self.db.run_in_transaction(callback, **kwargs)
        finally:
            self.fault.local.lane = None
            manager.put_session(session)


def harness(monkeypatch, mode='lands', *, unfixed=False, kind=CLAIM_KIND):
    store, db = fake_store()
    at = [0.]
    store.clock = lambda: 100.+at[0]
    store.wall = lambda: NOW_US/1e6+at[0]
    fault = SimpleNamespace(mode=mode, kind=kind, used=False, entered=threading.Event(),
        release=threading.Event(), commit=db._try_commit, local=threading.local())
    def commit(txn):
        lane = getattr(fault.local, 'lane', None)
        return lane.spanner_api.commit(txn) if lane else fault.commit(txn)
    monkeypatch.setattr(db, '_try_commit', commit)
    lanes = []
    def lane(name):
        result = NativeLane(db, at, fault, name)
        lanes.append(result)
        return result
    store.database = lane('publisher' if unfixed else 'observation')
    store.workspace_database = lane('workspace')
    store.transport = configure(store.database)
    store.workspace_transport = configure(store.workspace_database)
    def publisher_lane():
        if unfixed:
            return store
        result = copy.copy(store)
        result.database = lane('publisher')
        result.transport = configure(result.database)
        return result
    monkeypatch.setattr(store, 'publisher_lane', publisher_lane)
    # Offline session creation/recovery; native checkout, retirement, deadlines
    # and lock ownership remain real. Recovery waits until its 60s window.
    def prepare(manager, clock, boot=None):
        manager.managed = True
        if manager.ready:
            return True
        if not manager.recovery_due(clock):
            return False
        if not manager.lock.acquire(timeout=.05):
            return False
        try:
            manager.session = SimpleNamespace(delete=lambda: None)
            manager.retired = None
            manager.creation_unknown = False
            manager.next_recovery = clock()+60
            return True
        finally:
            manager.lock.release()
    monkeypatch.setattr(Sessions, 'prepare', prepare)
    for native in lanes:
        native._sessions_manager.next_recovery = 160.
    if unfixed:
        # Restore the terminal error branch on the CURRENT source, not a frozen
        # revision. Combined with the original shared lane this exposes the exit.
        source = textwrap.dedent(inspect.getsource(RuntimeStop.poll))
        source = source.replace('self.available = False\n        return False',
                                'self.available = False\n        self.stop()\n        return False')
        namespace = {}
        exec(compile(source, '<unfixed-poll-hook>', 'exec'), RuntimeStop.poll.__globals__, namespace)  # noqa: S102 - local current-source negative control
        monkeypatch.setattr(RuntimeStop, 'poll', namespace['poll'])
    svc = shell.Service(pins(), store, Attachments())
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    # Keep physical sleeps out of logical slot advancement; admission checks
    # and budgets still use real production code and the paired test clock.
    monkeypatch.setattr(shell, 'sleep', lambda seconds: None)
    monkeypatch.setattr(svc, 'trap_sigterm', lambda: None)
    ticks = Mock(wraps=svc.tick)
    monkeypatch.setattr(svc, 'tick', ticks)
    reasons = []
    async def close(reason='shutdown', **kwargs):
        reasons.append(reason)
        svc.stopped.set()
    monkeypatch.setattr(svc, 'close', close)
    def advance(seconds):
        at[0] = seconds
        db.server_ns = int(store.wall()*1e9)
    return svc, db, fault, ticks, reasons, advance


async def cleanup(svc, fault):
    fault.release.set()
    svc.stopped.set()
    await svc.runtime_stop.close()
    for task in (svc.task, svc.owner_task, svc.flush_job, svc.observer.task):
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    for executor in (svc.publisher_executor, svc.session_executor, svc.evidence_executor, svc.observer.executor):
        if executor is not None:
            await asyncio.to_thread(executor.shutdown, wait=True, cancel_futures=True)
    await asyncio.sleep(.01)  # Drain abandoned worker's stale callback.


def checkpoint(db):
    return sum(row.version for (kind, _), row in db.rows.items() if kind == COUNTER_KIND)


@pytest.mark.parametrize('stage', ['claim', 'publication'])
@pytest.mark.parametrize('unfixed', [True, False], ids=['unfixed', 'fixed'])
@pytest.mark.parametrize('mode', ['lands', 'absent', 'never'])
async def test_owner_progress_after_commit_deadline(monkeypatch, mode, unfixed, stage):
    kind, key = (CLAIM_KIND, 'local/publish') if stage == 'claim' else (HEALTH_KIND, 'local/fleet')
    svc, db, fault, ticks, reasons, advance = harness(monkeypatch, mode, unfixed=unfixed, kind=kind)
    try:
        fault.used = True
        svc.start()
        await until(lambda: checkpoint(db) > 0 and (HEALTH_KIND, 'local/fleet') in db.rows)
        before = checkpoint(db)
        fault.used = False
        advance(2.)
        await until(fault.entered.is_set)
        advance(2.500001)  # The commit has not returned by its 500ms cycle deadline.
        if mode != 'never':
            fault.release.set()
            await until(lambda: svc.state.job is None)
            assert svc.counters[next(iter(svc.counters))]['publisher']['unknown_outcomes'] == 1
            assert (json.loads(db.rows[(kind, key)].body)['slot'] == (NOW_US+2_000_000)//2_000_000) == (mode == 'lands')
        assert svc.port.capture_prediction('ws', int(svc.store.wall()*1e6)) is not None
        advance(4.)  # Poll the unavailable/occupied original session lane.
        await until(lambda: reasons if unfixed else ticks.call_count > 10)
        if unfixed:
            await until(lambda: svc.task.done())
            count = ticks.call_count
            advance(8.)
            await asyncio.sleep(.08)
            assert ticks.call_count == count and checkpoint(db) == before
            assert reasons == ['opt_out'] and not svc.policy_enabled()
            assert svc.runtime_stop.tripped is None
            assert not svc.bases.transport.objects.keys() & {item.key for item in svc.bases.queue}
        else:
            assert not reasons and svc.runtime_stop.available and svc.policy_enabled()
            # A completed unknown cycle recovers its lane; an unreturned worker
            # is quarantined by the watchdog. Neither can obstruct other lanes.
            advance(6.)
            await until(lambda: svc.incumbent_slot == (NOW_US+6_000_000)//2_000_000)
            await until(lambda: checkpoint(db) > before)
            assert ticks.call_count > 10 and svc.observer.counts['health_reads'] >= 2
            assert any(key.startswith('health-basis-batches/') for key in svc.bases.transport.objects)
            assert svc.counters[next(iter(svc.counters))]['publisher']['publications'] >= 1
            accounting(svc)
    finally:
        await cleanup(svc, fault)


@pytest.mark.parametrize('failure', ['poll', 'prepare', 'unready'])
async def test_start_failure_retries_without_cohort_admission(monkeypatch, failure):
    svc, db, fault, ticks, reasons, advance = harness(monkeypatch, 'absent')
    fault.used = True
    original = svc.store.read_runtime_stop if failure == 'poll' else svc.store.prepare_session
    calls = []
    def first_failure(*args):
        calls.append(1)
        if len(calls) == 1:
            if failure == 'unready':
                return False
            raise RuntimeError('offline startup failure')
        return original(*args)
    monkeypatch.setattr(svc.store, 'read_runtime_stop' if failure == 'poll' else 'prepare_session', first_failure)
    try:
        svc.start()
        await until(lambda: calls)
        assert not svc.runtime_stop.available
        assert svc.port.reserve(NOW_US) is None
        assert not ticks.call_count
        advance(4.)
        await until(lambda: len(calls) >= 2 and ticks.call_count, seconds=3.)
        await until(lambda: (HEALTH_KIND, 'local/fleet') in db.rows)
        assert svc.runtime_stop.available and not reasons
    finally:
        await cleanup(svc, fault)


@pytest.mark.parametrize('failure', ['suspended', 'exception'])
async def test_owner_backstop_recovers_suspended_owner_once(monkeypatch, caplog, failure):
    svc, db, fault, ticks, reasons, advance = harness(monkeypatch)
    fault.used = True
    run = svc.run
    started = asyncio.Event()
    calls = []
    async def suspended():
        calls.append(1)
        if len(calls) == 1:
            started.set()
            if failure == 'exception':
                raise RuntimeError('private exception content must not be logged')
            await asyncio.Event().wait()
        else:
            await run()
    monkeypatch.setattr(svc, 'run', suspended)
    try:
        svc.start()
        await started.wait()
        advance(shell.OWNER_PROGRESS_SECONDS+1)
        await until(lambda: len(calls) == 2, seconds=3.)
        await until(lambda: (HEALTH_KIND, 'local/fleet') in db.rows)
        await until(lambda: checkpoint(db) > 0)
        assert svc.counters[next(iter(svc.counters))]['diagnostics']['owner_recoveries'] == 1
        assert caplog.text.count('shadow_v2_owner_recovery') == 1
        assert 'private exception content' not in caplog.text
        assert not reasons
    finally:
        await cleanup(svc, fault)
