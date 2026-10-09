"""PR F1 SQL-sensitive faults, cap boundaries and propagation pins."""
# ruff: noqa: F811, F401 - shared fixtures
from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.fakes.spanner import _FakeSnapshot, _FakeTransaction
from tests.test_async_settle_handler import call, env, prepare, row_for
from tests.test_settle_outbox_drain import _typed_credit
from trusted_router import async_settle_ticket
from trusted_router import storage_gcp_async_admission as admission_sql
from trusted_router import storage_gcp_counter_dml as counters
from trusted_router.services.async_settle import (
    CACHE_SECONDS,
    TIER_CAPS,
    Admission,
    AdmissionCache,
    DrainHealth,
)
from trusted_router.services.async_settle_handler import admission_reason
from trusted_router.services.settle_outbox_drain import drain_settle_outbox
from trusted_router.storage_gcp_async_settle import async_reservation_admission_statement


@pytest.mark.parametrize('tier,default', [(1, 0), (2, 25_000_000), (3, 100_000_000)])
@pytest.mark.parametrize('pilot', [0, 5_000_000, 200_000_000])
@pytest.mark.parametrize('delta', [-1, 0, 1])
def test_cap_semantics(env, tier, default, pilot, delta):
    cap = pilot or default
    amount = max(0, cap + delta)
    cache = AdmissionCache(lambda _: Admission(amount, tier), clock=lambda: 10)
    cache.health = DrainHealth(10, 0)
    env[2].admission = cache
    assert cache.eligible('ws-v1', pilot) is (tier != 1 and amount <= cap)
    reason = admission_reason(env[2], 'ws-v1', pilot)
    if tier == 1:
        assert reason == 'not_eligible'
    elif amount > cap:
        assert reason == 'cap_exceeded'
    assert TIER_CAPS == {2: 25_000_000, 3: 100_000_000}


def test_two_replica_stale_cap(env):
    clock = [10.0]
    amount = [0]
    def read(_):
        return Admission(amount[0], 2)
    old, new = [AdmissionCache(read, clock=lambda: clock[0]) for _ in range(2)]
    old.health = new.health = DrainHealth(10, 0)
    assert old.eligible('ws', 0)
    clock[0] = 14.9
    amount[0] = 25_000_001
    assert not new.eligible('ws', 0)
    assert old.eligible('ws', 0)  # Different process ages genuinely disagree below five seconds.
    clock[0] = 15
    old.health = new.health = DrainHealth(15, 0)
    assert not old.eligible('ws', 0) and not new.eligible('ws', 0)


@pytest.mark.parametrize('predicate', ['claim_not_exists', 'atomic_settled', 'refresh', 'sparse', 'control_kind', 'control_id', 'sentinel'])
def test_fake_rejects_dropped_predicate(env, predicate):
    store, db, _, _ = env
    body, auth, _ = prepare(env)
    row = row_for(env, body)
    pt = store._param_types
    if predicate == 'claim_not_exists':
        statement = counters.claim_reservation_statement(pt, auth.credit_reservation_id,
            actual_micro=2, settled_usage_type='Credits', async_fence=True)
        needle = 'AND NOT EXISTS'
    elif predicate == 'atomic_settled':
        statement = async_reservation_admission_statement(pt, row)
        needle = 'AND settled=false'
    elif predicate == 'sentinel':
        statement = admission_sql.admission_statement('ws-v1')
        needle = 'LIMIT 1001'
    elif predicate == 'sparse':
        statement = admission_sql.unresolved_statement()
        needle = 'unresolved_at IS NOT NULL'
    elif predicate.startswith('control'):
        statement = admission_sql.control_statement('fleet-v1')
        needle = 'kind=@kind' if predicate == 'control_kind' else 'id=@id'
    else:
        db.expect_async_refresh_fence = True
        store.settle_outbox.enqueue(row)
        statements = []
        original = _FakeTransaction.execute_update
        # Capture actual refresh SQL without changing its semantics.
        from unittest.mock import patch
        def record(self, sql, **kwargs):
            statements.append((sql, kwargs.get('params', {}), kwargs.get('param_types', {})))
            return original(self, sql, **kwargs)
        with patch.object(_FakeTransaction, 'execute_update', record):
            store.settle_outbox.enqueue(row)
        statement = next(s for s in statements if 'async_version IS NULL' in s[0])
        needle = 'AND async_version IS NULL'
    sql, params, types = statement
    assert needle in sql
    tx = _FakeTransaction(db)
    execute = tx.execute_sql if sql.startswith('SELECT') else tx.execute_update
    execute(sql, params=params, param_types=types)
    tx = _FakeTransaction(db)
    execute = tx.execute_sql if sql.startswith('SELECT') else tx.execute_update
    with pytest.raises(AssertionError, match='missing|predicate'):
        execute(sql.replace(needle, ''), params=params, param_types=types)


def test_admission_fake_counts_all_unresolved(env):
    store, db, _, _ = env
    body, _, _ = prepare(env)
    credit = _typed_credit(db, 'ws-v1')
    credit['trust_tier'] = 2
    row = row_for(env, body)
    for i, (status, leased, created) in enumerate([
        ('pending', None, '2026-10-06T00:00:00Z'),
        ('pending', '2099-01-01T00:00:00Z', None), ('dead', None, None), ('done', None, None),
    ]):
        value = row.model_dump() if hasattr(row, 'model_dump') else vars(row).copy()
        value.update(authorization_id=str(i), status=status, leased_until=leased, created_at=created,
                     actual_cost_micro=(i+1)*10)
        db.settle_outbox[(str(i), 'settle')] = value
    assert admission_sql.read_admission(db, 'ws-v1') == Admission(60, 2)
    for i in range(1001):
        db.settle_outbox[(f'overflow-{i}', 'settle')] = dict(value, status='pending', actual_cost_micro=0)
    with pytest.raises(ValueError, match='admission unavailable'):
        admission_sql.read_admission(db, 'ws-v1')


@pytest.mark.parametrize('boundary', ['before_commit', 'lost_commit', 'after_commit'])
def test_transaction_fault_retry_identity(env, monkeypatch, boundary):
    from google.api_core.exceptions import DeadlineExceeded

    from tests.fakes.spanner import FakeSpannerDatabase
    body, auth, _ = prepare(env)
    original = FakeSpannerDatabase._try_commit
    attempts = []
    trace = []
    update = _FakeTransaction.execute_update
    read = _FakeSnapshot.execute_sql
    def record_insert(tx, sql, **kwargs):
        if sql.startswith('INSERT INTO tr_settle_outbox'):
            params = kwargs['params']
            trace.append(('insert', params['authorization_id'], params['intent_kind']))
        return update(tx, sql, **kwargs)
    def record_lookup(snapshot, sql, **kwargs):
        params = kwargs.get('params', {})
        if 'tr_settle_outbox' in sql and params.get('aid') == auth.id:
            trace.append(('read', params['aid'], params.get('kind')))
        return read(snapshot, sql, **kwargs)
    monkeypatch.setattr(_FakeTransaction, 'execute_update', record_insert)
    monkeypatch.setattr(_FakeSnapshot, 'execute_sql', record_lookup)
    def fail_once(db, tx):
        if db is not env[1]:
            return original(db, tx)
        attempts.append(copy.deepcopy(tx.pending_writes))
        if len(attempts) == 1 and boundary == 'before_commit':
            raise DeadlineExceeded('before commit')
        result = original(db, tx)
        if len(attempts) == 1:
            if boundary == 'after_commit':
                raise SystemExit('crash after durable commit')
            raise DeadlineExceeded('commit response lost')
        return result
    with monkeypatch.context() as fault:
        fault.setattr(FakeSpannerDatabase, '_try_commit', fail_once)
        if boundary == 'after_commit':
            with pytest.raises(SystemExit):
                call(env, body)
        else:
            assert call(env, body).status_code == 202
    saved = copy.deepcopy(env[1].settle_outbox)
    assert call(env, body).status_code == 202
    assert env[1].settle_outbox == saved
    inserts = [entry for entry in trace if entry[0] == 'insert']
    assert len(inserts) >= 2 and set(inserts) == {('insert', auth.id, 'settle')}
    last_insert = max(i for i, entry in enumerate(trace) if entry[0] == 'insert')
    assert ('read', auth.id, 'settle') in trace[last_insert+1:]
    assert list(saved) == [(auth.id, 'settle')]
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 0
    assert drain_settle_outbox(10)['outcomes'] == {'settled_now': 1}
    assert call(env, body).status_code == 200
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 2


def test_typed_unavailable_holds_and_recovers(env, monkeypatch):
    from trusted_router.storage_errors import StoreUnavailable
    body, auth, _ = prepare(env)
    assert call(env, body).status_code == 202
    before = copy.deepcopy(env[1].reservations[auth.credit_reservation_id])
    with monkeypatch.context() as fault:
        def unavailable(*a, **kw):
            raise StoreUnavailable('F1 injected typed outage')
        fault.setattr(type(env[0]), 'typed_finalize_gateway', unavailable)
        result = drain_settle_outbox(10)
    assert result['outcomes'] == {'park_typed_unavailable': 1}
    assert env[1].reservations[auth.credit_reservation_id] == before
    row = env[1].settle_outbox[(auth.id, 'settle')]
    assert row['status'] == 'pending' and row['attempts'] == 0 and row['settle_body'] is not None
    row['next_attempt_at'] = '2000-01-01T00:00:00Z'
    assert drain_settle_outbox(10)['outcomes'] == {'settled_now': 1}
    assert row is not env[1].settle_outbox[(auth.id, 'settle')]
    assert env[1].settle_outbox[(auth.id, 'settle')]['settle_body'] is None


def test_local_ttl_pins():
    from trusted_router.config import Settings
    from trusted_router.services import settle_outbox_drain
    from trusted_router.storage_gcp_credit_shards import DEFAULT_CACHE_TTL_SECONDS
    assert CACHE_SECONDS == 5 <= 300
    assert async_settle_ticket.MAX_TTL_SECONDS == 300
    assert DEFAULT_CACHE_TTL_SECONDS == 60 <= 300
    assert settle_outbox_drain._DRAIN_LEASE_SECONDS == 300
    cfg = Settings(environment='test')
    assert cfg.async_settle_ticket_ttl_seconds <= 300
    assert cfg.settle_outbox_lease_seconds == 300
    assert cfg.settle_outbox_health_publish_interval_seconds == 2


@pytest.mark.parametrize('authority', ['federated', 'deferred_home'])
@pytest.mark.parametrize('kind', ['settle', 'refund'])
def test_federation_excluded_from_async(env, authority, kind):
    from tests.test_async_settle_proof import save
    from tests.test_settle_outbox_drain import _client
    from trusted_router import billing_snapshot as billing
    from trusted_router.services.async_settle import snapshot_projection

    body, auth, _ = prepare(env, kind=kind)
    snapshot = billing.parse_snapshot(json.dumps(body['billing_snapshot']))
    for auth_authority, observed_authority in ((authority, 'local'), ('local', authority)):
        auth.settlement = auth_authority
        projection = snapshot_projection(
            authorization=auth, snapshot=snapshot,
            requested=billing.Eligibility(authority=observed_authority),
            runtime=env[2], settings=env[3])
        assert projection == {'async_eligible': False}  # No ticket can be issued.
    # Even an otherwise valid local ticket cannot accept a federated request.
    body['observed']['authority'] = authority
    before = save(env[1])
    client = _client(env[3])
    client.app.state.async_settle = env[2]
    response = client.post('/v1/internal/gateway/' + kind, json=body,
                           headers={'X-TR-Settlement-Mode': 'async-v1'})
    client.close()
    assert response.status_code == 200, response.text
    assert response.json()['data']['acceptance']['status'] == 'sync_required'
    assert response.json()['data']['reason'] == 'unsupported_cohort'
    assert save(env[1]) == before


def test_insert_uniqueness_and_preserve_existing(env):
    import time

    from google.api_core.exceptions import AlreadyExists

    from tests.fakes.spanner import FakeAlreadyExists
    from trusted_router.storage_gcp_async_settle import enqueue
    body, auth, _ = prepare(env)
    box = env[0].settle_outbox
    row = row_for(env, body)
    enqueue(box, row, time.monotonic()+.5)
    before = copy.deepcopy(env[1].settle_outbox)
    with pytest.raises((AlreadyExists, FakeAlreadyExists)):
        enqueue(box, row, time.monotonic()+.5)
    assert env[1].settle_outbox == before
    # Legacy immutable mode is independent of the async refresh fence.
    box._async_fence = False
    changed = copy.deepcopy(row)
    changed.actual_cost_micro = 999
    assert box.enqueue(changed, preserve_existing=True) == 'frozen'
    assert env[1].settle_outbox == before


def test_concurrent_sibling_refund_full_money(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier, Lock
    body, auth, _ = prepare(env)
    refund = copy.deepcopy(body)
    refund['terminal'].update(terminal_kind='refund', charge_micro=0)
    assert call(env, body).status_code == 202
    assert call(env, refund).status_code == 202
    barrier, lock = Barrier(2), Lock()
    arrived = []
    from tests.fakes.spanner import FakeSpannerDatabase
    original = FakeSpannerDatabase._try_commit
    def overlap(db, tx):
        if db is env[1] and any(op[0] == 'update_reservation' and op[2].get('settled')
                               for op in tx.pending_writes):
            with lock:
                wait = len(arrived) < 2
                arrived.append(tx)
            if wait:
                barrier.wait(timeout=1)
        return original(db, tx)
    with monkeypatch.context() as fault:
        fault.setattr(FakeSpannerDatabase, '_try_commit', overlap)
        with ThreadPoolExecutor(max_workers=2) as pool:
            charge = pool.submit(call, env, body, synchronous=True)
            release = pool.submit(call, env, refund, synchronous=True)
            results = (charge.result(timeout=30), release.result(timeout=30))
    assert [r.status_code for r in results] == [200, 200]
    reservation = env[1].reservations[auth.credit_reservation_id]
    assert reservation['settled'] and reservation['actual_micro'] in (0, 2)
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == reservation['actual_micro']
    assert _typed_credit(env[1], 'ws-v1')['reserved'] == 0
    drain_settle_outbox(10)
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == reservation['actual_micro']
    refund_row = env[1].settle_outbox[(auth.id, 'refund')]
    settle_row = env[1].settle_outbox[(auth.id, 'settle')]
    assert refund_row['status'] == 'done' and refund_row['settle_body'] is None
    assert settle_row['status'] == ('done' if reservation['actual_micro'] == 2 else 'dead')
    assert (settle_row['settle_body'] is None) is (reservation['actual_micro'] == 2)


def test_auxiliary_cache_and_health_cadence_pins():
    from tests.test_async_settle_drain import healthy
    from trusted_router import storage_gcp_authorize
    from trusted_router.routes.internal import gateway
    from trusted_router.services import federation

    assert federation.NEGATIVE_TTL_SECONDS == 60 <= 300
    assert storage_gcp_authorize._OUTBOX_ABSENT_CACHE_SECONDS == 5 <= 300
    assert gateway._BROADCAST_EMPTY_CACHE_TTL_SECONDS == 60 <= 300
    now = [10.0]
    evidence = [healthy(observed_at=10, worker_heartbeat=10)]
    cache = AdmissionCache(lambda _: Admission(0, 2), clock=lambda: now[0],
                           wall_clock=lambda: now[0], health_read=lambda: evidence[0])
    assert cache.eligible('ws', 0)
    evidence[0] = None
    now[0] = 10.999
    assert cache.eligible('ws', 0)
    now[0] = 11.0
    assert not cache.eligible('ws', 0)


def test_finalize_commit_crash_before_mark(env, monkeypatch):
    from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
    body, auth, _ = prepare(env)
    assert call(env, body).status_code == 202
    with monkeypatch.context() as fault:
        def crash(*a, **kw):
            raise SystemExit('F1 crash after finalize commit before done mark')
        fault.setattr(SpannerSettleOutbox, 'mark', crash)
        with pytest.raises(SystemExit):
            drain_settle_outbox(10)
    row = env[1].settle_outbox[(auth.id, 'settle')]
    assert row['status'] == 'pending' and row['settle_body'] is not None
    before = copy.deepcopy(env[1].typed)
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 2
    assert env[1].reservations[auth.credit_reservation_id]['settled']
    # Move the old lease into the past; PR D separately pins TTL and equality.
    row['leased_until'] = '2000-01-01T00:00:00Z'
    result = drain_settle_outbox(10)
    assert result['outcomes'] == {'already_settled_with_charge': 1}
    assert env[1].typed == before
    row = env[1].settle_outbox[(auth.id, 'settle')]
    assert row['status'] == 'done' and row['settle_body'] is None and row['terminal_at'] is not None


@pytest.mark.parametrize('kind', ['settle', 'refund'])
@pytest.mark.parametrize('deleted_key', [False, True])
def test_F1_001_retention_money_transaction(env, monkeypatch, kind, deleted_key):
    """Both stamps share the money commit, roll back with it, and use finalize now."""
    from tests.fakes.spanner import FakeSpannerDatabase
    from tests.test_async_settle_proof import retention_clock, save, terminal_time

    body, auth, key = prepare(env, kind=kind)
    db = env[1]
    if deleted_key:
        db.typed['tr_key_limit'].clear()
        db.rows.pop(('api_key', key.hash), None)
    now = retention_clock(monkeypatch)
    before = save(db)
    update = _FakeTransaction.execute_update
    commit = FakeSpannerDatabase._try_commit
    statements = {}
    money_commits = []
    fail = True

    def record(tx, sql, **kwargs):
        count = update(tx, sql, **kwargs)
        statements.setdefault(tx, []).append((sql, kwargs['params'], count))
        if fail and sql.startswith('UPDATE tr_reservation SET terminal_at=IF('):
            raise RuntimeError('F1-001 rollback after terminal stamps')
        return count

    def check_commit(database, tx):
        writes = statements.get(tx, [])
        if any(sql.startswith('UPDATE tr_reservation SET settled=true') and count == 1
               for sql, _, count in writes):
            stamps = [(sql, params, count) for sql, params, count in writes
                      if 'SET terminal_at=IF(' in sql]
            assert len(stamps) == 2
            assert [count for _, _, count in stamps] == [1, 1]
            assert [params['now'] for _, params, _ in stamps] == [now, now]
            assert [params['kind'] for _, params, _ in stamps] == [kind, kind]
            assert [params['record_id'] for _, params, _ in stamps] == [auth.id, auth.credit_reservation_id]
            assert any('UPDATE tr_credit_balance' in sql and count == 1 for sql, _, count in writes)
            money_commits.append(writes)
        return commit(database, tx)

    monkeypatch.setattr(_FakeTransaction, 'execute_update', record)
    monkeypatch.setattr(FakeSpannerDatabase, '_try_commit', check_commit)
    with pytest.raises(RuntimeError, match='F1-001 rollback'):
        call(env, body, synchronous=True)
    assert save(db) == before
    fail = False
    assert call(env, body, synchronous=True).status_code == 200
    assert len(money_commits) == 1
    assert terminal_time(db.reservations[auth.credit_reservation_id]) == now
    assert terminal_time(db.gateway_authorizations[auth.id]) == now
    settled = save(db)
    assert call(env, body, synchronous=True).status_code == 200
    assert save(db) == settled
    assert len(money_commits) == 1
