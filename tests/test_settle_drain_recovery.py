"""Watchdog recovery and protected mixed-sibling state differentials."""
# ruff: noqa: F811, F401 - shared real Spanner fixtures
from __future__ import annotations

import copy

import pytest

from tests.test_async_settle_drain import seed
from tests.test_async_settle_handler import call, env, prepare, row_for
from tests.test_settle_outbox_drain import fake_store
from trusted_router.config import Settings
from trusted_router.services import settle_outbox_drain as drain
from trusted_router.services.settle_outbox_apply import ApplyOutcome


def test_mixed_sibling_keeps_open_reservation_pending(env, monkeypatch):
    store, db, _, cfg = env
    body, auth, _ = prepare(env)
    row = row_for(env, body)
    row.async_version = None
    store.settle_outbox.enqueue(row)
    refund = copy.deepcopy(body)
    refund['terminal'].update(terminal_kind='refund', charge_micro=0)
    assert call(env, refund).status_code == 202
    alerts = []
    monkeypatch.setattr(drain, 'ops_alert', lambda *a, alerts=alerts, **kw: alerts.append((a, kw)))
    row = store.settle_outbox.claim_shard(
        shard=db.settle_outbox[(auth.id, 'settle')]['queue_shard'], lease_seconds=30)[0]
    outcome = drain.apply_frozen_settle(row)
    drain._resolve_row(drain.spanner_settle_outbox(), row, outcome, error_note=None)
    assert not db.reservations[auth.credit_reservation_id]['settled']
    assert outcome == ApplyOutcome.PARK_TYPED_UNAVAILABLE
    assert db.settle_outbox[(auth.id, 'settle')]['status'] == 'pending'
    assert alerts == []


def test_legacy_async_prefix_cannot_hide_null_version(fake_store):
    box = seed(fake_store[0], 3)
    db = fake_store[1]
    for i in (0, 1):
        db.settle_outbox[(f'fast-{i}', 'settle')].update(
            async_version=1, next_attempt_at='2000-01-01T00:00:00Z')
    db.settle_outbox[('fast-2', 'settle')]['next_attempt_at'] = '2001-01-01T00:00:00Z'
    assert [r.authorization_id for r in box.claim(limit=1, lease_seconds=300)] == ['fast-2']
    assert all(db.settle_outbox[(f'fast-{i}', 'settle')]['lease_owner'] is None for i in (0, 1))
    query = db.snapshot_sql[-1]
    assert 'AND async_version IS NULL' in query
    assert 'FORCE_INDEX=tr_settle_outbox_due_v2' in query
    assert db.snapshot_sql_params[-1]['limit'] == 2


def test_idle_watchdog_recovery_is_sixteen_bounded_reads(fake_store, monkeypatch):
    from tests.fakes.spanner import _FakeSnapshot
    calls = []
    original = _FakeSnapshot.execute_sql
    def execute(self, sql, **kw):
        if 'queue_shard=@shard' in sql:
            calls.append((sql, kw))
        assert not ('kind=@kind AND id=@id' in sql and kw.get('params', {}).get('id') == 'fleet-v1')
        return original(self, sql, **kw)
    monkeypatch.setattr(_FakeSnapshot, 'execute_sql', execute)
    drain.drain_settle_outbox(500)
    assert len(calls) == 16
    assert {kw['params']['shard'] for _, kw in calls} == set(range(16))
    assert all(kw['params']['limit'] == 2 and kw['timeout'] == .2 and kw['retry'] is None
               and kw['request_options']['priority'] == 'PRIORITY_LOW' for _, kw in calls)


@pytest.mark.parametrize('age,heartbeat_age,valid,expected', [
    (0, 0, True, False), (4.999, 4.999, True, False), (5, 0, True, True),
    (0, 5, True, True), (-1, 0, True, True), (0, -1, True, True),
    (0, 0, False, True), (None, None, True, True),
])
def test_watchdog_ownership_uses_only_strict_freshness(env, monkeypatch, age, heartbeat_age, valid, expected):
    from tests.test_async_settle_drain import healthy
    from trusted_router.storage_gcp_async_admission import HEALTH_ID, _write_control, read_health
    body, auth, _ = prepare(env)
    assert call(env, body).status_code == 202
    now = 1_800_000_000.
    if age is not None:
        evidence = healthy(observed_at=now-age, worker_heartbeat=now-heartbeat_age,
                           p95_age_seconds=99, oldest_unresolved_age_seconds=100, complete=False)
        if not valid:
            evidence['backlog_count'] = 'invalid'
        env[1].run_in_transaction(lambda tx: _write_control(tx, HEALTH_ID, evidence))
    monkeypatch.setattr(drain.time, 'time', lambda: now)
    before = copy.deepcopy(env[1].rows.get(('settle_drain_control', 'fleet-v1')))
    result = drain.drain_settle_outbox(10)
    assert result['claimed'] == int(expected)
    assert env[1].reservations[auth.credit_reservation_id]['settled'] is expected
    assert env[1].rows.get(('settle_drain_control', 'fleet-v1')) == before
    if expected:
        assert result['outcomes'] == {'settled_now': 1}


def test_watchdog_unreadable_health_recovers_short_lease(env, monkeypatch):
    from trusted_router import storage_gcp_async_admission as health
    from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox
    body, auth, _ = prepare(env)
    assert call(env, body).status_code == 202
    def fail(*a):
        raise TimeoutError('private-workspace')
    monkeypatch.setattr(health, 'read_health', fail)
    original = SpannerSettleOutbox.claim_candidates
    leases = []
    def claim(self, candidates, **kw):
        leases.append(kw['lease_seconds'])
        return original(self, candidates, **kw)
    monkeypatch.setattr(SpannerSettleOutbox, 'claim_candidates', claim)
    result = drain.drain_settle_outbox(10, settings=Settings(environment='test'))
    assert result['outcomes'] == {'settled_now': 1}
    assert leases == [30]


def test_watchdog_protection_off_leaves_async_and_logs_count(env, caplog):
    body, auth, _ = prepare(env)
    assert call(env, body).status_code == 202
    env[3].async_settle_protection = False
    before = copy.deepcopy(env[1].settle_outbox)
    result = drain.drain_settle_outbox(10)
    assert result['claimed'] == 0
    assert env[1].settle_outbox == before
    assert 'async_recovery.protection_disabled due_count_at_least=1' in caplog.text
    assert auth.id not in caplog.text


@pytest.mark.parametrize('winner_kind', ['settle', 'refund'])
def test_terminal_mixed_sibling_matches_unprotected_null_sibling(env, monkeypatch, winner_kind):
    import datetime as dt

    from trusted_router import storage_gcp_settle_outbox as adapter
    from trusted_router.services.async_settle_handler import finish
    store, db, _, cfg = env
    body, auth, _ = prepare(env)
    refund = copy.deepcopy(body)
    refund['terminal'].update(terminal_kind='refund', charge_micro=0)
    winner_body = body if winner_kind == 'settle' else refund
    loser_body = refund if winner_kind == 'settle' else body
    loser = row_for(env, loser_body)
    loser.async_version = None
    store.settle_outbox.enqueue(loser)
    assert call(env, winner_body).status_code == 202
    stamp = dt.datetime.now(dt.UTC)
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp
    monkeypatch.setattr(dt, 'datetime', Clock)
    monkeypatch.setattr(adapter, '_iso_now', lambda: stamp.isoformat().replace('+00:00', 'Z'))
    names = ('typed', 'rows', 'reservations', 'gateway_authorizations', 'settle_outbox',
             'generation_records', 'operational_analytics_outbox', 'analytics_outbox', 'reservation_idemp')
    saved = {name: copy.deepcopy(getattr(db, name)) for name in names}
    results = []
    for protection in (False, True):
        for name, value in saved.items():
            setattr(db, name, copy.deepcopy(value))
        cfg.async_settle_protection = protection
        store.settle_outbox._async_fence = protection
        if not protection:
            db.settle_outbox[(auth.id, winner_kind)]['async_version'] = None
        box = drain.spanner_settle_outbox()
        winner = box.get(auth.id, winner_kind)
        winner_outcome = drain.apply_frozen_settle(winner)
        assert winner_outcome == ApplyOutcome.SETTLED_NOW
        drain._resolve_row(box, winner, winner_outcome, error_note=None)
        assert db.reservations[auth.credit_reservation_id]['settled']
        alerts = []
        with monkeypatch.context() as patch:
            patch.setattr(drain, 'ops_alert', lambda *a, alerts=alerts, **kw: alerts.append((a, kw)))
            outcome = drain.apply_frozen_settle(loser)
            drain._resolve_row(box, loser, outcome, error_note=None)
        state = {name: copy.deepcopy(getattr(db, name)) for name in names}
        # The single intentional input difference is the winner's version.
        assert state['settle_outbox'][(auth.id, winner_kind)]['async_version'] == (1 if protection else None)
        state['settle_outbox'][(auth.id, winner_kind)]['async_version'] = None
        results.append((outcome, alerts, state))
    assert results[0] == results[1]
    assert results[1][0] == (ApplyOutcome.ALREADY_SETTLED_WITH_CHARGE
                            if winner_kind == 'settle' else ApplyOutcome.ALREADY_RELEASED_FREE)
    assert bool(results[1][1]) is (winner_kind == 'refund')


def test_protection_off_reports_seen_rows_even_when_recovery_budget_expires(fake_store, monkeypatch, caplog):
    box = seed(fake_store[0], 1)
    fake_store[1].settle_outbox[('fast-0', 'settle')].update(async_version=1, queue_shard=0)
    clock = iter([0., 2.])
    monkeypatch.setattr(drain, '_monotonic', lambda: next(clock))
    assert list(drain._recover_async(box, limit=1, deadline=1., settings=None)) == []
    assert 'async_recovery.protection_disabled due_count_at_least=1' in caplog.text
    assert fake_store[1].settle_outbox[('fast-0', 'settle')]['lease_owner'] is None
