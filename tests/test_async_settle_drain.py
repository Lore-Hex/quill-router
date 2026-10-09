# ruff: noqa: F811, F401 - shared fixture
from __future__ import annotations

import datetime as dt
import json
import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

import pytest

from tests.fakes.spanner import _FakeSnapshot, make_fake_store
from tests.test_settle_outbox_drain import _bare_authorization, _outbox, _row, fake_store
from trusted_router.config import Settings
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.services import settle_outbox_worker as worker
from trusted_router.services.async_settle import (
    Admission,
    AdmissionCache,
    decode_health,
    load_runtime,
)
from trusted_router.services.settle_outbox_apply import ApplyOutcome
from trusted_router.storage_gcp_async_admission import (
    HEALTH_ID,
    HEALTH_KIND,
    HEALTH_PUBLISH_ID,
    claim_health_publish,
    claim_housekeeping,
    publish_health,
    read_health,
    unresolved_statement,
)
from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox


def settings(**kw):
    return Settings(environment='test', settle_outbox_fast_drain_enabled=True,
                    **kw)


def seed(store, count=8):
    box = _outbox(store)
    for i in range(count):
        box.enqueue(_row(_bare_authorization(f'fast-{i}')))
    return box


def healthy(**overrides):
    return dict(dict(v=1, authority='local', observed_at=100., worker_heartbeat=100.,
                     complete=True, p95_age_seconds=1., backlog_count=1,
                     sample_count=1, frozen_micro=7, dead_count=0,
                     p50_age_seconds=.5, oldest_unresolved_age_seconds=2.), **overrides)


@pytest.mark.parametrize('state', ['fresh', 'stale', 'missing'])
@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('heartbeat', [False, True])
def test_health_freshness_matrix(state, empty, heartbeat):
    value = healthy()
    if empty:
        value.update(backlog_count=0, sample_count=0, frozen_micro=0, p95_age_seconds=0.,
                     p50_age_seconds=0., oldest_unresolved_age_seconds=0.)
    if not heartbeat:
        value.pop('worker_heartbeat')
    if state == 'stale':
        value['observed_at'] = 95.
    if state == 'missing':
        value = None
    cache = AdmissionCache(lambda ws: Admission(0, 2), clock=lambda: 40.,
                           wall_clock=lambda: 100., health_read=lambda: value)
    assert cache.eligible('ws', 0) is (state == 'fresh' and heartbeat)


@pytest.mark.parametrize('overrides', [dict(worker_heartbeat=94), dict(worker_heartbeat=101),
    dict(observed_at=101), dict(complete=False), dict(p95_age_seconds=6),
    dict(p95_age_seconds=float('nan')), dict(sample_count=0), dict(backlog_count=True),
    dict(frozen_micro=-1), dict(worker_heartbeat=None), dict(observed_at=float('inf'))])
def test_health_invalid_evidence(overrides):
    assert decode_health(healthy(**overrides), now=40., wall=100.) is None


def test_health_cache_never_rejuvenates_and_refresh_failure_closes():
    clock = [100.]
    reads = []
    def read():
        reads.append(clock[0])
        if clock[0] >= 102:
            raise TimeoutError
        return healthy(observed_at=96., worker_heartbeat=99.)
    cache = AdmissionCache(lambda ws: Admission(0, 2), clock=lambda: clock[0],
                           wall_clock=lambda: clock[0], health_read=read)
    assert cache.eligible('ws', 0)
    clock[0] = 100.9
    assert cache.eligible('ws', 0)
    assert len(reads) == 1
    clock[0] = 101.
    assert not cache.eligible('ws', 0)  # freshly read OLD row is still stale
    clock[0] = 102.
    assert not cache.eligible('ws', 0)
    assert cache.health is None


def test_consumer_off_no_rpc_and_on_bounded_refresh(monkeypatch):
    from trusted_router import storage_gcp_async_admission as adapter
    store, db = make_fake_store()
    calls = []
    original = _FakeSnapshot.execute_sql
    def spy(self, sql, **kwargs):
        calls.append((sql, kwargs))
        return original(self, sql, **kwargs)
    monkeypatch.setattr(_FakeSnapshot, 'execute_sql', spy)
    publish_health(db)
    calls.clear()
    monkeypatch.setattr(adapter, 'read_admission', lambda *a: Admission(0, 2))
    cfg = Settings(environment='test')
    runtime = load_runtime(cfg, store)
    assert runtime.admission is not None
    assert not runtime.admission.eligible('ws', 0)
    assert calls == []
    cfg = Settings(environment='test', async_settle_enabled=True, async_settle_protection=True)
    cache = load_runtime(cfg, store).admission
    assert cache is not None and cache.eligible('ws', 0)
    assert len(calls) == 1
    sql, options = calls[0]
    assert sql == 'SELECT body FROM tr_entities WHERE kind=@kind AND id=@id'
    assert options['params'] == dict(kind=HEALTH_KIND, id=HEALTH_ID)
    assert options['timeout'] == .5 and options['retry'] is None
    assert options['request_options'] == {'priority': 'PRIORITY_LOW'}
    assert cache.eligible('ws', 0) and len(calls) == 1
    cfg.async_settle_enabled = False
    cache.health_read_at = float('-inf')
    assert not cache.eligible('ws', 0) and len(calls) == 1


def test_publisher_includes_leased_dead_and_legacy_no_workspace(fake_store, monkeypatch):
    store, db = fake_store
    box = seed(store, 4)
    now = dt.datetime.now(dt.UTC)
    for i, record in enumerate(db.settle_outbox.values()):
        record['created_at'] = (now - dt.timedelta(seconds=10+i)).isoformat()
    [leased] = box.claim(limit=1)
    assert box.mark('fast-1', 'settle', done=False, force_dead=True) == 'dead'
    assert box.mark('fast-2', 'settle', done=True) == 'done'
    value = publish_health(db)
    assert value['backlog_count'] == value['sample_count'] == 3
    assert value['frozen_micro'] == 3 * 777777
    assert value['dead_count'] == 1 and value['complete']
    assert value['worker_heartbeat'] >= value['observed_at']
    assert value['oldest_unresolved_age_seconds'] >= 13
    assert read_health(db) == value
    assert box.get(leased.authorization_id, 'settle').lease_owner == leased.lease_owner
    from trusted_router import storage_gcp_async_admission as adapter
    monkeypatch.setattr(adapter, 'HEALTH_ROW_LIMIT', 1)
    assert publish_health(db)['complete'] is False


def test_publish_empty_has_heartbeat(fake_store):
    value = publish_health(fake_store[1])
    assert fake_store[1].last_timeout_secs == .5
    assert 'worker_heartbeat' in value
    assert value['worker_heartbeat'] >= value['observed_at']
    assert value['sample_count'] == value['backlog_count'] == value['frozen_micro'] == 0
    assert value['complete']
    assert decode_health(read_health(fake_store[1]), now=time.monotonic(), wall=time.time())


@pytest.mark.parametrize('claim', [claim_housekeeping, lambda db: claim_health_publish(db, 2)])
def test_control_claim_transaction_deadline(fake_store, claim):
    db = fake_store[1]
    assert claim(db)
    assert db.last_timeout_secs == .5


def test_housekeeping_cadence_over_fast_polls(fake_store, monkeypatch):
    counts = []
    clock = [1000.]
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    from trusted_router.storage_gcp_authorize import ReapPassResult
    reaps = []
    monkeypatch.setattr(SpannerSettleOutbox, 'purge_done', lambda *a: counts.append(clock[0]) or 0)
    def reap(*a, **kw):
        reaps.append(clock[0])
        assert kw['limit'] == 200
        return ReapPassResult(count=0)
    monkeypatch.setattr(type(fake_store[0]), 'reap_expired_reservations_result', reap)
    for poll in range(31):
        clock[0] = 1000 + poll * 10
        worker.drain_pass(1, settings=settings())
    assert counts == reaps == [1000, 1300]
    # Independent replicas race on the same complete PK; only one wins.
    clock[0] = 1600
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: claim_housekeeping(fake_store[1]), range(6)))
    assert sum(results) == 1


def test_concurrent_claims_and_owner_crash_fences(fake_store, monkeypatch):
    from trusted_router import storage_gcp_settle_outbox as module
    store, db = fake_store
    box = seed(store, 16)
    stamp = ['2026-10-06T01:00:00+00:00']
    for row in db.settle_outbox.values():
        row['next_attempt_at'] = '2026-10-05T00:00:00+00:00'
    monkeypatch.setattr(module, '_iso_now', lambda: stamp[0])
    lease = '2026-10-06T01:00:10+00:00'
    candidate = box.due(limit=1)[0]
    barrier = threading.Barrier(6)
    def claim(i):
        barrier.wait()
        return box._claim_one(replace(candidate), owner=f'owner-{i}', lease_until=lease)
    with ThreadPoolExecutor(max_workers=6) as pool:
        winners = list(pool.map(claim, range(6)))
    assert sum(winners) == 1
    owner = f'owner-{winners.index(True)}'
    assert not box._claim_one(candidate, owner='takeover', lease_until=lease)
    assert box.mark(candidate.authorization_id, 'settle', done=True, lease_owner='wrong') is None
    assert not box.park(candidate.authorization_id, 'settle', lease_owner='wrong')
    stamp[0] = lease  # strict less-than, not <=
    assert not box._claim_one(candidate, owner='takeover', lease_until=lease)
    stamp[0] = '2026-10-06T01:00:11+00:00'
    assert box._claim_one(candidate, owner='takeover', lease_until='2026-10-06T01:01:00+00:00')
    assert box.mark(candidate.authorization_id, 'settle', done=True, lease_owner=owner) is None
    assert not box.park(candidate.authorization_id, 'settle', lease_owner=owner)
    assert box.mark(candidate.authorization_id, 'settle', done=True, lease_owner='takeover') == 'done'


def test_claims_never_exceed_running_slots(fake_store, monkeypatch):
    store, db = fake_store
    seed(store, 24)
    lock = threading.Lock()
    active = set()
    seen = []
    original = SpannerSettleOutbox.claim_shard
    def claim(self, **kwargs):
        # Account claims, not only applies: executor-queued leases count too.
        with lock:
            rows = original(self, **kwargs)
            for row in rows:
                assert row.authorization_id not in seen
                seen.append(row.authorization_id)
                active.add(row.authorization_id)
            assert len(active) <= 3
            return rows
    def apply(row):
        time.sleep(.05)
        with lock:
            active.remove(row.authorization_id)
        return ApplyOutcome.SETTLED_NOW
    monkeypatch.setattr(SpannerSettleOutbox, 'claim_shard', claim)
    monkeypatch.setattr(drain, 'apply_frozen_settle', apply)
    result = worker.drain_pass(24, settings=settings(settle_outbox_worker_concurrency=3))
    assert result['claimed'] == len(seen) == 24
    assert len(set(seen)) == 24 and not active
    assert all(r['status'] == 'done' for r in db.settle_outbox.values())
    shards = {p['shard'] for p in db.snapshot_sql_params if 'shard' in p}
    assert shards == set(range(16))


@pytest.mark.parametrize('expired', ['lease', 'budget'])
def test_batch_tail_expired_claim_never_applied(monkeypatch, expired):
    row = _row(_bare_authorization('tail'))
    row.leased_until = '2000-01-01T00:00:00Z' if expired == 'lease' else '2099-01-01T00:00:00Z'
    clock = iter([0., 2.])
    monkeypatch.setattr(worker.time, 'monotonic', lambda: next(clock, 2.))
    calls = []
    monkeypatch.setattr(drain, 'apply_frozen_settle', lambda row: calls.append(row) or ApplyOutcome.SETTLED_NOW)
    monkeypatch.setattr(drain, '_resolve_row', lambda *args, **kw: None)
    box = SimpleNamespace(claim_shard=lambda **kw: [row], _database=None, _pt=None, _async_fence=False)
    assert worker._apply_slot(box, 0, 30, 1. if expired == 'budget' else 10.) == (1, 'deferred', 0)
    assert calls == []


def test_settings_and_loop_stop(fake_store, monkeypatch):
    defaults = Settings(environment='test')
    expected = dict(settle_outbox_fast_drain_enabled=False, settle_outbox_poll_interval_seconds=300,
                    settle_outbox_health_publish_interval_seconds=2,
                    settle_outbox_claim_batch=500, settle_outbox_worker_concurrency=1,
                    settle_outbox_lease_seconds=300, settle_outbox_pass_budget_seconds=240)
    assert {k: getattr(defaults, k) for k in expected} == expected
    for kwargs in [dict(settle_outbox_worker_concurrency=33), dict(settle_outbox_claim_batch=0),
                   dict(settle_outbox_health_publish_interval_seconds=0),
                   dict(settle_outbox_health_publish_interval_seconds=5.01),
                   dict(settle_outbox_health_publish_interval_seconds=float('nan')),
                   dict(settle_outbox_lease_seconds=30, settle_outbox_pass_budget_seconds=30)]:
        with pytest.raises(ValueError):
            settings(**kwargs)
    with pytest.raises(ValueError):
        worker.run_worker(defaults, threading.Event())
    calls = []
    class Stop:
        def is_set(self):
            return len(calls) == 3
        def wait(self, interval):
            assert interval == .01
    monkeypatch.setattr(worker, 'drain_pass', lambda *a, **kw: calls.append(kw['start_shard']))
    worker.run_worker(settings(settle_outbox_poll_interval_seconds=.01), Stop())
    assert calls == [0, 1, 2]


def test_docs_literal_is_builder_fixture():
    import re
    from pathlib import Path
    root = Path(__file__).parents[1]
    doc = (root/'docs/design/async-settle-outbox-v1.md').read_text().split('### 3.1')[1].split('### 3.2')[0]
    fixture = json.loads((root/'tests/fixtures/async_settlement/authorize_v1_builder.json').read_text())
    blocks = [json.loads(block) for block in re.findall(r'```json\n(.*?)\n```', doc, re.S)]
    assert blocks == [fixture['claims'], fixture['response']]


@pytest.mark.parametrize('replicas,polls,poll,interval', [(1, 9, 1., 2.), (6, 17, .25, 2.), (4, 21, .5, 5.)])
def test_health_publish_cadence_across_workers(fake_store, monkeypatch, replicas, polls, poll, interval):
    db = fake_store[1]
    clock = [1000.]
    observations = []
    lock = threading.Lock()
    original = _FakeSnapshot.execute_sql
    def query(self, sql, **kwargs):
        if 'FORCE_INDEX=tr_settle_outbox_unresolved' in sql:
            with lock:
                observations.append(clock[0])
        return original(self, sql, **kwargs)
    monkeypatch.setattr(_FakeSnapshot, 'execute_sql', query)
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    monkeypatch.setattr(worker, 'claim_housekeeping', lambda db: False)
    cfg = settings(settle_outbox_health_publish_interval_seconds=interval)
    with ThreadPoolExecutor(max_workers=replicas) as pool:
        for tick in range(polls):
            clock[0] = 1000. + tick * poll
            barrier = threading.Barrier(replicas)
            def run(_, barrier=barrier):
                barrier.wait()
                return worker.drain_pass(1, settings=cfg)
            assert all(result['claimed'] == 0 for result in pool.map(run, range(replicas)))
    assert len(observations) == math.ceil(polls * poll / interval)
    assert all(b - a >= interval for a, b in zip(observations[:-1], observations[1:], strict=True))
    assert read_health(db)['observed_at'] == observations[-1]
    claim = json.loads(db.rows[(HEALTH_KIND, HEALTH_PUBLISH_ID)].body)
    assert claim['observed_at'] == observations[-1]


@pytest.mark.parametrize('failure', [False, True])
def test_health_claim_loss_or_failure_preserves_apply(fake_store, monkeypatch, failure):
    monkeypatch.setattr(time, 'time', lambda: 1000.)
    store, db = fake_store
    seed(store, 1)
    assert claim_health_publish(db, 2)
    assert not claim_health_publish(db, 2)
    monkeypatch.setattr(worker, 'claim_housekeeping', lambda db: False)
    if failure:
        def fail(*args):
            raise TimeoutError
        monkeypatch.setattr(worker, 'claim_health_publish', fail)
    publications = []
    monkeypatch.setattr(worker, 'publish_health', lambda db: publications.append(1))
    monkeypatch.setattr(drain, 'apply_frozen_settle', lambda row: ApplyOutcome.SETTLED_NOW)
    result = worker.drain_pass(1, settings=settings())
    assert result['claimed'] == 1 and result['outcomes'] == {ApplyOutcome.SETTLED_NOW: 1}
    assert db.settle_outbox[('fast-0', 'settle')]['status'] == 'done'
    assert publications == []


def test_newer_health_observation_wins(fake_store, monkeypatch):
    db = fake_store[1]
    clock = [1000.]
    entered, release = threading.Event(), threading.Event()
    original = _FakeSnapshot.execute_sql
    def query(self, sql, **kwargs):
        if 'FORCE_INDEX=tr_settle_outbox_unresolved' in sql and clock[0] == 1000.:
            entered.set()
            assert release.wait(10)
        return original(self, sql, **kwargs)
    monkeypatch.setattr(time, 'time', lambda: clock[0])
    monkeypatch.setattr(_FakeSnapshot, 'execute_sql', query)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(publish_health, db)
        try:
            assert entered.wait(10)
            clock[0] = 1002.
            newer = publish_health(db)
        finally:
            release.set()
        assert old.result()['observed_at'] == 1000.
    assert read_health(db) == newer


def test_sparse_health_query_orders_before_limit_and_tracks_status(fake_store):
    store, db = fake_store
    box = seed(store, 4)
    for i, row in enumerate(db.settle_outbox.values()):
        row['created_at'] = f'2026-01-01T00:00:0{i}+00:00'
    assert box.mark('fast-0', 'settle', done=True) == 'done'
    assert box.mark('fast-2', 'settle', done=False, force_dead=True) == 'dead'
    sql, params, types = unresolved_statement()
    assert sql == ('SELECT unresolved_at, actual_cost_micro, status FROM tr_settle_outbox'
                   '@{FORCE_INDEX=tr_settle_outbox_unresolved} '
                   'WHERE unresolved_at IS NOT NULL ORDER BY unresolved_at LIMIT @limit')
    assert params == {'limit': 10001}
    with db.snapshot() as snapshot:
        rows = list(snapshot.execute_sql(sql, params={'limit': 2}, param_types=types))
    assert rows == [['2026-01-01T00:00:01+00:00', 777777, 'pending'],
                    ['2026-01-01T00:00:02+00:00', 777777, 'dead']]


def test_admission_read_emits_no_workspace_diagnostic(monkeypatch, caplog):
    import logging
    from contextlib import nullcontext

    from trusted_router.storage_gcp_async_admission import read_admission
    db = SimpleNamespace(snapshot=lambda: nullcontext(SimpleNamespace(execute_sql=lambda *a, **kw: [[1, 42, 2]])))
    with caplog.at_level(logging.DEBUG):
        assert read_admission(db, 'private-workspace-id') == Admission(42, 2)
    assert caplog.records == []


@pytest.mark.parametrize('status', ['pending', 'dead'])
def test_null_created_at_is_visible_and_unhealthy(fake_store, status):
    store, db = fake_store
    seed(store, 1)
    row = db.settle_outbox[('fast-0', 'settle')]
    row.update(created_at=None, status=status)
    value = publish_health(db)
    assert value['sample_count'] == value['backlog_count'] == 1
    assert value['frozen_micro'] == 777777
    assert value['dead_count'] == (status == 'dead')
    assert value['oldest_unresolved_age_seconds'] >= value['observed_at']
    assert value['complete'] is False
    assert decode_health(read_health(db), now=time.monotonic(), wall=time.time()) is None


def test_inserted_done_has_no_unresolved_index_entry(fake_store):
    store, db = fake_store
    from trusted_router.storage_gcp_settle_outbox import intent_insert_statements
    box = _outbox(store)
    statements = intent_insert_statements(box._pt, _row(_bare_authorization('inline-done')),
        now=dt.datetime.now(dt.UTC).isoformat(), next_attempt_at=None, resolved=True)
    def insert(transaction):
        for sql, params, types in statements:
            transaction.execute_update(sql, params=params, param_types=types)
    db.run_in_transaction(insert)
    assert db.settle_outbox[('inline-done', 'settle')]['status'] == 'done'
    sql, params, types = unresolved_statement()
    with db.snapshot() as snapshot:
        assert list(snapshot.execute_sql(sql, params=params, param_types=types)) == []
    value = publish_health(db)
    assert value['backlog_count'] == value['sample_count'] == value['frozen_micro'] == 0
    assert decode_health(value, now=time.monotonic(), wall=time.time()) is not None


@pytest.mark.parametrize('concurrency', [1, 4])
def test_shutdown_stops_new_claims_and_finishes_inflight(fake_store, monkeypatch, concurrency):
    store, db = fake_store
    seed(store, 16)
    # Ensure the first slot has work; the other 15 records must remain byte-identical.
    for i, row in enumerate(db.settle_outbox.values()):
        row['queue_shard'] = i
    before = {key: dict(row) for key, row in db.settle_outbox.items()}
    stop = threading.Event()
    original = SpannerSettleOutbox.claim_shard
    original_slot = worker._apply_slot
    def slot(outbox, shard, lease, deadline, event):
        # Hold other slots at their entry until the first claim sets stop.
        if shard != 0:
            assert stop.wait(2)
        return original_slot(outbox, shard, lease, deadline, event)
    monkeypatch.setattr(worker, '_apply_slot', slot)
    claims, applies = [], []
    def claim(self, **kw):
        assert not stop.is_set()
        result = original(self, **kw)
        claims.extend(result)
        stop.set()
        return result
    monkeypatch.setattr(SpannerSettleOutbox, 'claim_shard', claim)
    monkeypatch.setattr(drain, 'apply_frozen_settle', lambda row: applies.append(row) or ApplyOutcome.SETTLED_NOW)
    monkeypatch.setattr(worker, 'claim_housekeeping', lambda db: False)
    monkeypatch.setattr(worker, 'claim_health_publish', lambda *args: False)
    started = time.monotonic()
    worker.run_worker(settings(settle_outbox_worker_concurrency=concurrency), stop)
    assert time.monotonic() - started < 2
    assert len(claims) == len(applies) == 1
    assert db.settle_outbox[('fast-0', 'settle')]['status'] == 'done'
    assert {key: row for key, row in db.settle_outbox.items() if key != ('fast-0', 'settle')} == {
        key: row for key, row in before.items() if key != ('fast-0', 'settle')}


# Exercise the direct decoder AND the durable reader/cache boundary.
_BAD_HEALTH = [
    ('incomplete', {'complete': False}),
    ('stale-heartbeat', {'worker_heartbeat': 94.}),
    ('observed-after-heartbeat', {'worker_heartbeat': 99.}),
    ('over-p95', {'p95_age_seconds': 5.0001, 'oldest_unresolved_age_seconds': 6.}),
    ('empty-with-money', {'backlog_count': 0, 'sample_count': 0}),
    ('count-mismatch', {'sample_count': 0}),
    ('dead-over-backlog', {'dead_count': 2}),
    ('percentile-order', {'p50_age_seconds': 1.5}),
    ('oldest-order', {'oldest_unresolved_age_seconds': .9}),
    ('wrong-authority', {'authority': 'remote'}),
    ('extra-key', {'extra': 0}),
    ('huge-json', {'authority': 'x' * 5000}),
    ('huge-integer-json', {'frozen_micro': 10 ** 4100}),
    ('wrong-version', {'v': 2}),
    ('bool-version', {'v': True}),
    ('wrong-complete', {'complete': 1}),
]
for _field in ('sample_count', 'backlog_count', 'dead_count', 'frozen_micro'):
    for _label, _bad in [('negative', -1), ('bool', True), ('float', 1.), ('string', '1'), ('null', None)]:
        _BAD_HEALTH.append((f'{_field}-{_label}', {_field: _bad}))
for _field in ('observed_at', 'worker_heartbeat', 'p50_age_seconds', 'p95_age_seconds',
               'oldest_unresolved_age_seconds'):
    for _label, _bad in [('negative', -1.), ('nan', float('nan')), ('inf', float('inf')),
                         ('negative-inf', float('-inf')), ('bool', True), ('string', '1'), ('null', None)]:
        _BAD_HEALTH.append((f'{_field}-{_label}', {_field: _bad}))


@pytest.mark.parametrize('case,overrides', _BAD_HEALTH, ids=[case for case, _ in _BAD_HEALTH])
def test_adversarial_health_is_ineligible(fake_store, case, overrides):
    db = fake_store[1]
    publish_health(db)
    value = healthy(**overrides)
    db.rows[(HEALTH_KIND, HEALTH_ID)].body = json.dumps(value)
    assert decode_health(value, now=40., wall=100.) is None
    assert decode_health(read_health(db), now=40., wall=100.) is None
    cache = AdmissionCache(lambda ws: Admission(0, 2), clock=lambda: 40.,
                           wall_clock=lambda: 100., health_read=lambda: read_health(db))
    assert not cache.eligible('ws', 0)


@pytest.mark.parametrize('field', list(healthy()))
def test_every_health_field_is_required(fake_store, field):
    db = fake_store[1]
    publish_health(db)
    value = healthy()
    del value[field]
    db.rows[(HEALTH_KIND, HEALTH_ID)].body = json.dumps(value)
    assert read_health(db) is None
    assert decode_health(value, now=40., wall=100.) is None


@pytest.mark.parametrize('case', ['wrong-kind', 'wrong-id', 'missing', 'huge-json', 'broken-json', 'duplicate-key'])
def test_health_wire_evidence_fails_closed(fake_store, case):
    db = fake_store[1]
    publish_health(db)
    row = db.rows.pop((HEALTH_KIND, HEALTH_ID))
    if case == 'wrong-kind':
        db.rows[('wrong-kind', HEALTH_ID)] = row
    elif case == 'wrong-id':
        db.rows[(HEALTH_KIND, 'wrong-id')] = row
    elif case != 'missing':
        row.body = {'huge-json': ' ' * 5000 + json.dumps(healthy()), 'broken-json': '{',
                    'duplicate-key': json.dumps(healthy())[:-1] + ', "v": 1}'}[case]
        db.rows[(HEALTH_KIND, HEALTH_ID)] = row
    assert read_health(db) is None
    assert decode_health(read_health(db), now=40., wall=100.) is None


@pytest.mark.parametrize('column,bad', [('status', 'done'), ('status', None), ('actual_cost_micro', True),
    ('actual_cost_micro', -1), ('actual_cost_micro', float('nan')), ('created_at', None),
    ('created_at', 'bad'), ('created_at', '2999-01-01T00:00:00Z')])
def test_publisher_rejects_invalid_observation(fake_store, monkeypatch, column, bad):
    record = dict(created_at=dt.datetime.now(dt.UTC), actual_cost_micro=7, status='pending')
    record[column] = bad
    original = _FakeSnapshot.execute_sql
    def query(self, sql, **kwargs):
        if 'FORCE_INDEX=tr_settle_outbox_unresolved' in sql:
            return [[record[k] for k in ('created_at', 'actual_cost_micro', 'status')]]
        return original(self, sql, **kwargs)
    monkeypatch.setattr(_FakeSnapshot, 'execute_sql', query)
    value = publish_health(fake_store[1])
    assert value['complete'] is False
    assert decode_health(value, now=time.monotonic(), wall=time.time()) is None


@pytest.mark.parametrize('overrides', [dict(authority='remote'), dict(extra=0), dict(dead_count=2),
    dict(p50_age_seconds=3.), dict(complete=1), dict(sample_count=0), dict(frozen_micro=-1), dict(v=True)])
def test_publisher_validates_previous_health(fake_store, monkeypatch, overrides):
    db = fake_store[1]
    publish_health(db)
    # Malformed future records must not pin publication via their timestamps.
    previous = healthy(observed_at=1001., worker_heartbeat=1001., **overrides)
    db.rows[(HEALTH_KIND, HEALTH_ID)].body = json.dumps(previous)
    monkeypatch.setattr(time, 'time', lambda: 1000.)
    value = publish_health(db)
    assert read_health(db) == value


def test_shutdown_finishes_all_four_inflight_applies(fake_store, monkeypatch):
    store, db = fake_store
    seed(store, 16)
    for i, row in enumerate(db.settle_outbox.values()):
        row['queue_shard'] = i
    stop = threading.Event()
    ready = threading.Barrier(4)
    applied = []
    def apply(row):
        ready.wait(timeout=3)
        stop.set()
        applied.append(row.authorization_id)
        return ApplyOutcome.SETTLED_NOW
    monkeypatch.setattr(drain, 'apply_frozen_settle', apply)
    monkeypatch.setattr(worker, 'claim_housekeeping', lambda db: False)
    monkeypatch.setattr(worker, 'claim_health_publish', lambda *args: False)
    worker.run_worker(settings(settle_outbox_worker_concurrency=4), stop)
    assert set(applied) == {f'fast-{i}' for i in range(4)}
    assert sum(row['status'] == 'done' for row in db.settle_outbox.values()) == 4
    assert all(row['lease_owner'] is None for row in list(db.settle_outbox.values())[4:])
