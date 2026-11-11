from __future__ import annotations

import copy
import json
import logging
import threading
import time
from contextvars import ContextVar
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from google.api_core.exceptions import AlreadyExists, DeadlineExceeded
from pydantic import ValidationError

from tests.test_async_settle_drain import healthy
from tests.test_async_settle_handler import NOW, _drain_async, call, prepare, row_for
from tests.test_async_settle_handler import env as env  # noqa: F401
from tests.test_async_settle_ticket import settings
from tests.test_settle_outbox_drain import _typed_credit, _typed_key
from trusted_router import billing_snapshot as billing
from trusted_router import storage_gcp_io
from trusted_router.async_settle_ticket import verify_lookup_ticket
from trusted_router.config import Settings
from trusted_router.services import async_settle_handler as handler
from trusted_router.services import async_settle_memory as memory
from trusted_router.services.async_settle import (
    CACHE_SECONDS,
    Admission,
    AdmissionCache,
    DrainHealth,
)
from trusted_router.services.async_settle_admission_refresh import (
    ACTIVE_SECONDS,
    AdmissionRefresher,
    install,
)
from trusted_router.storage_gcp_async_settle import ReservationNotOpen


class Clock:
    def __init__(self):
        self.value = time.monotonic()

    def __call__(self):
        return self.value


class SharedClockTime:
    """``time`` for storage_gcp_io whose monotonic clock is the test's Clock.

    MemoryHandoff computes RPC deadlines from its injected clock, and
    spanner_rpc_deadline checks them against storage_gcp_io's monotonic
    clock. With a frozen fake clock and the real monotonic clock, a runner
    stalled for more than ATTEMPT_SECONDS after the clock was created saw
    every writer attempt expire before enqueue, which write_once records as
    a retry. Installed only around the writer's calls: the request path
    computes its own deadlines from the real clock.
    """

    def __init__(self, clock):
        self.monotonic = clock

    def __getattr__(self, name):
        return getattr(time, name)


class ParkedThread:
    ident = None
    alive = True

    def is_alive(self):
        return self.alive


@pytest.fixture
def ram(env):
    body, _, _ = prepare(env)
    env[3].async_settle_handoff_mode = 'memory'
    clock = Clock()
    writer = memory.MemoryHandoff(clock=clock)
    writer.thread = ParkedThread()
    env[2].memory = writer
    return body, writer, clock


def reason(response):
    return json.loads(response.body)['data']['reason']


def status(response):
    return json.loads(response.body)['data']['acceptance']['status']


def forbidden(*args, **kwargs):
    raise AssertionError('storage must not be called by a memory handoff')


class ForbiddenStore:
    _database = object()  # Backend capability metadata, not a database call.

    def __getattr__(self, name):
        forbidden()


@pytest.fixture
def admission_rig():
    clock = Clock()
    read = Mock(return_value=Admission(0, 2))
    health = Mock(return_value=healthy(observed_at=100, worker_heartbeat=100))
    cache = AdmissionCache(read, clock=clock, health_read=health, wall_clock=lambda: 100)
    return SimpleNamespace(clock=clock, read=read, health=health, cache=cache,
                           worker=AdmissionRefresher(cache))


def test_cold_cached_only_miss_warms_via_read_ahead(admission_rig):
    rig = admission_rig
    assert not rig.cache.eligible('cold', 0, cached_only=True)
    rig.read.assert_not_called()
    rig.health.assert_not_called()
    rig.worker.refresh_once(now=rig.clock())
    rig.read.assert_called_once_with('cold')
    rig.health.assert_called_once_with()
    assert rig.cache.entries['cold'] == (rig.clock(), Admission(0, 2))
    assert rig.cache.eligible('cold', 0, cached_only=True)
    rig.read.assert_called_once_with('cold')
    rig.health.assert_called_once_with()


def test_stale_cached_only_miss_reactivates_read_ahead(admission_rig):
    rig = admission_rig
    stale = (rig.clock() - CACHE_SECONDS - 1, Admission(0, 2))
    rig.cache.entries['stale'] = stale
    rig.cache.evaluated_at['stale'] = rig.clock() - ACTIVE_SECONDS - 1
    assert not rig.cache.eligible('stale', 0, cached_only=True)
    assert rig.cache.entries['stale'] == stale
    rig.read.assert_not_called()
    rig.health.assert_not_called()
    rig.worker.refresh_once(now=rig.clock())
    rig.read.assert_called_once_with('stale')
    assert rig.cache.eligible('stale', 0, cached_only=True)
    rig.read.assert_called_once_with('stale')


def test_cached_only_miss_at_capacity_adds_no_entry(admission_rig):
    rig = admission_rig
    entries = {str(i): (rig.clock(), Admission(0, 2)) for i in range(4096)}
    rig.cache.entries = entries.copy()
    assert not rig.cache.eligible('cold', 0, cached_only=True)
    assert rig.cache.entries == entries
    assert rig.cache.evaluated_at['cold'] == rig.clock()
    rig.read.assert_not_called()
    rig.health.assert_not_called()
    rig.worker.refresh_once(now=rig.clock())
    rig.read.assert_not_called()
    assert 'cold' not in rig.cache.evaluated_at


@pytest.mark.parametrize('cached_only', [False, True])
@pytest.mark.parametrize('fresh_health', [False, True])
def test_placeholder_is_never_admission_evidence(admission_rig, cached_only, fresh_health):
    rig = admission_rig
    rig.cache.entries['cold'] = (float('-inf'), None)
    if fresh_health:
        rig.cache.health = DrainHealth(rig.clock(), 0)
    runtime = SimpleNamespace(admission=rig.cache)
    assert handler.admission_reason(runtime, 'cold', 0) == 'admission_stale'
    rig.read.side_effect = RuntimeError('no evidence available')
    assert not rig.cache.eligible('cold', 0, cached_only=cached_only)
    assert rig.read.call_count == (0 if cached_only else 1)


def test_full_path_reads_placeholder_once(admission_rig):
    rig = admission_rig
    rig.cache.entries['cold'] = (float('-inf'), None)
    assert rig.cache.eligible('cold', 0)
    assert rig.cache.entries['cold'] == (rig.clock(), Admission(0, 2))
    assert rig.cache.eligible('cold', 0)
    rig.read.assert_called_once_with('cold')
    rig.health.assert_called_once_with()


def test_full_path_capacity_prunes_placeholder(admission_rig):
    rig = admission_rig
    rig.cache.entries = {str(i): (rig.clock(), Admission(0, 2)) for i in range(4095)}
    rig.cache.entries['placeholder'] = (float('-inf'), None)
    rig.cache.evaluated_at['placeholder'] = rig.clock()
    assert rig.cache.eligible('new', 0)
    assert len(rig.cache.entries) == 4096
    assert 'placeholder' not in rig.cache.entries
    assert 'placeholder' not in rig.cache.evaluated_at
    rig.read.assert_called_once_with('new')


@pytest.mark.parametrize('value', [Admission(0, 0), Admission(11, 1), Admission(11, 2), Admission(-1, 2)])
def test_placeholder_refresh_preserves_tier_and_amount_checks(admission_rig, value):
    rig = admission_rig
    rig.read.return_value = value
    assert not rig.cache.eligible('cold', 10, cached_only=True)
    rig.worker.refresh_once(now=rig.clock())
    rig.read.assert_called_once_with('cold')
    assert not rig.cache.eligible('cold', 10, cached_only=True)
    rig.read.assert_called_once_with('cold')
    if value.pending_micro < 0:
        assert rig.cache.entries['cold'] == (float('-inf'), None)


@pytest.mark.parametrize('initial_reader', ['full', 'read_ahead'])
@pytest.mark.parametrize('cached_only', [True, False])
def test_health_failure_preserves_cached_evidence_until_expiry(admission_rig, initial_reader, cached_only):
    rig = admission_rig
    rig.clock.value = 0
    if initial_reader == 'full':
        assert rig.cache.eligible('initial', 0)
    else:
        assert not rig.cache.eligible('initial', 0, cached_only=True)
        rig.worker.refresh_once(now=0)
    evidence = rig.cache.health
    assert evidence == DrainHealth(0, 1)
    rig.clock.value = 2
    # Read-ahead supplies fresh workspace evidence so only health expires at 5.
    assert not rig.cache.eligible('fresh', 0, cached_only=True)
    rig.health.side_effect = RuntimeError('health unavailable')
    rig.worker.refresh_once(now=2)
    assert rig.cache.entries['fresh'] == (2, Admission(0, 2))
    assert rig.cache.health is evidence and rig.cache.health_read_at == 0
    assert rig.health.call_count == 2  # success at 0, read-ahead failure at 2
    assert rig.cache.eligible('fresh', 0, cached_only=cached_only) is cached_only
    if not cached_only:
        assert rig.cache.health is None  # unchanged full-path failure rule
        assert rig.health.call_count == 3
        return
    rig.clock.value = 4.9
    assert rig.cache.eligible('fresh', 0, cached_only=True)
    rig.clock.value = 5.0
    assert not rig.cache.eligible('fresh', 0, cached_only=True)
    assert rig.cache.health is evidence
    assert rig.health.call_count == 2
    assert rig.read.call_count == 2


@pytest.mark.parametrize('initial_reader', ['full', 'read_ahead'])
def test_shared_health_failure_and_read_ahead_recovery(admission_rig, initial_reader):
    rig = admission_rig
    rig.clock.value = 0
    rig.cache.wall_clock = lambda: 100 + rig.clock()
    assert (rig.health.call_count, rig.read.call_count) == (0, 0)
    if initial_reader == 'full':
        assert rig.cache.eligible('workspace', 0)
    else:
        assert not rig.cache.eligible('workspace', 0, cached_only=True)
        assert (rig.health.call_count, rig.read.call_count) == (0, 0)
        rig.worker.refresh_once(now=0)
    assert rig.cache.health == DrainHealth(0, 1)
    assert rig.cache.health_read_at == 0
    assert (rig.health.call_count, rig.read.call_count) == (1, 1)
    assert rig.cache.eligible('workspace', 0, cached_only=True)
    assert (rig.health.call_count, rig.read.call_count) == (1, 1)
    # Finish before workspace read-ahead is due (3s) or evidence expires (5s).
    # Health read-ahead reads only when its last attempt is at least
    # HEALTH_SECONDS (0.5s) old and the slot is empty or that old: hence the
    # reads at 2.25 (slot cleared at 2) and 2.75 (0.5s after 2.25) below.
    workspace_evidence = rig.cache.entries['workspace']
    assert workspace_evidence == (0, Admission(0, 2))

    rig.clock.value = 2
    rig.health.side_effect = RuntimeError('health unavailable')
    assert not rig.cache.eligible('workspace', 0)
    assert rig.cache.health is None
    assert rig.cache.health_read_at == 2
    assert (rig.health.call_count, rig.read.call_count) == (2, 1)
    assert not rig.cache.eligible('workspace', 0, cached_only=True)
    assert (rig.health.call_count, rig.read.call_count) == (2, 1)

    rig.clock.value = 2.25
    rig.worker.refresh_once(now=2.25)
    assert (rig.health.call_count, rig.read.call_count) == (3, 1)
    assert rig.cache.health is None
    assert rig.cache.health_read_at == 2
    assert not rig.cache.eligible('workspace', 0, cached_only=True)
    assert (rig.health.call_count, rig.read.call_count) == (3, 1)

    rig.clock.value = 2.75
    rig.health.side_effect = None
    rig.health.return_value = healthy(observed_at=102.75, worker_heartbeat=102.75)
    rig.worker.refresh_once(now=2.75)
    assert (rig.health.call_count, rig.read.call_count) == (4, 1)
    assert rig.cache.health == DrainHealth(2.75, 1)
    assert rig.cache.health_read_at == 2.75
    assert rig.cache.eligible('workspace', 0, cached_only=True)
    assert (rig.health.call_count, rig.read.call_count) == (4, 1)
    assert rig.cache.entries['workspace'] is workspace_evidence


def test_unhealthy_read_ahead_record_closes_cached_only_admission(admission_rig):
    # A valid, fresh record of an unhealthy drain is a successful read: it
    # replaces the healthy evidence at once instead of being kept out like a
    # failed read, so cached-only admission closes without waiting for expiry.
    # Health read-ahead reads only when its last attempt is at least
    # HEALTH_SECONDS (0.5s) old and the slot is empty or that old: hence the
    # reads at 0, 0.5 and 1.0 below.
    rig = admission_rig
    rig.clock.value = 0
    rig.cache.wall_clock = lambda: 100 + rig.clock()
    assert not rig.cache.eligible('workspace', 0, cached_only=True)
    rig.worker.refresh_once(now=0)
    assert rig.cache.health == DrainHealth(0, 1)
    assert rig.cache.eligible('workspace', 0, cached_only=True)
    assert (rig.health.call_count, rig.read.call_count) == (1, 1)

    rig.clock.value = 0.5
    rig.health.return_value = healthy(observed_at=100.5, worker_heartbeat=100.5,
                                      p95_age_seconds=6, oldest_unresolved_age_seconds=6)
    rig.worker.refresh_once(now=0.5)
    assert (rig.health.call_count, rig.read.call_count) == (2, 1)
    assert rig.cache.health == DrainHealth(0.5, 6)
    assert rig.cache.health_read_at == 0.5
    assert not rig.cache.eligible('workspace', 0, cached_only=True)
    assert handler.admission_reason(SimpleNamespace(admission=rig.cache), 'workspace', 0) == 'drain_unhealthy'
    assert (rig.health.call_count, rig.read.call_count) == (2, 1)

    rig.clock.value = 1.0
    rig.health.return_value = healthy(observed_at=101.0, worker_heartbeat=101.0)
    rig.worker.refresh_once(now=1.0)
    assert (rig.health.call_count, rig.read.call_count) == (3, 1)
    assert rig.cache.health == DrainHealth(1.0, 1)
    assert rig.cache.eligible('workspace', 0, cached_only=True)
    assert (rig.health.call_count, rig.read.call_count) == (3, 1)


@pytest.mark.parametrize('reader', ['full', 'read_ahead'])
def test_health_evidence_keeps_its_observation_time(admission_rig, reader):
    # Evidence is as old as the drain's observation, not as the read: a record
    # read 4.5 s after it was observed admits for only 0.5 s more, through
    # either reader, with no further read.
    rig = admission_rig
    rig.clock.value = 0
    rig.cache.wall_clock = lambda: 100 + rig.clock()
    rig.health.return_value = healthy(observed_at=95.5, worker_heartbeat=100)
    if reader == 'full':
        assert rig.cache.eligible('workspace', 0)
    else:
        assert not rig.cache.eligible('workspace', 0, cached_only=True)
        rig.worker.refresh_once(now=0)
    assert rig.cache.health == DrainHealth(-4.5, 1)
    assert (rig.health.call_count, rig.read.call_count) == (1, 1)
    rig.clock.value = 0.4
    assert rig.cache.eligible('workspace', 0, cached_only=True)
    rig.clock.value = 0.5
    assert not rig.cache.eligible('workspace', 0, cached_only=True)
    assert (rig.health.call_count, rig.read.call_count) == (1, 1)


def different_valid_payload(env, body):
    changed = copy.deepcopy(body)
    changed['raw_usage']['output_tokens'] = 12
    value = handler.parse(json.dumps(changed).encode())
    evaluated = billing.evaluate(value.snapshot, value.terminal.selected_endpoint,
                                 value.raw, value.observed)
    changed['terminal'].update(usage=evaluated.usage.model_dump(), charge_micro=evaluated.charge_micro)
    original, alternate = row_for(env, body), row_for(env, changed)
    assert original.actual_cost_micro == 2 and alternate.actual_cost_micro == 7
    assert original.payload_hash != alternate.payload_hash
    return changed


def memory_commit_outcomes(caplog):
    return [record.getMessage().split('outcome=')[1].split()[0]
            for record in caplog.records
            if record.name == memory.logger.name
            and record.getMessage().startswith('metric=async_settle_memory_commit ')]


def assert_settled_once(env, body):
    """Check money and the winning payload, then replay without a second charge."""
    store, db, _, _ = env
    terminal = body['terminal']
    def state():
        auth = store.get_gateway_authorization(terminal['authorization_id'])
        assert auth.settled
        reservation = db.reservations[auth.credit_reservation_id]
        assert reservation['settled'] and reservation['actual_micro'] == terminal['charge_micro']
        credit = _typed_credit(db, 'ws-v1')
        assert credit['total_usage'] == terminal['charge_micro'] and credit['reserved'] == 0
        assert _typed_key(db, terminal['key_id'])['usage'] == terminal['charge_micro']
        generation = store.get_generation(auth.finalized_generation_id)
        assert generation.total_cost_microdollars == terminal['charge_micro']
        assert generation.tokens_prompt == terminal['usage']['total_prompt_tokens']
        assert generation.tokens_completion == terminal['usage']['output_tokens']
        return auth.finalized_generation_id
    generation_id = state()
    assert call(env, body, synchronous=True).status_code == 200
    assert _drain_async(concurrency=1)['outcomes'] == {}
    assert state() == generation_id


@pytest.mark.parametrize('different', [False, True])
def test_two_memory_instances_share_one_durable_winner(env, ram, caplog, different):
    body, first, clock = ram
    other_body = different_valid_payload(env, body) if different else copy.deepcopy(body)
    second = memory.MemoryHandoff(clock=clock)
    second.thread = ParkedThread()
    other_env = (env[0], env[1], replace(env[2], memory=second), env[3])
    caplog.set_level(logging.INFO, logger=memory.logger.name)
    assert call(env, body).status_code == 202
    assert call(other_env, other_body).status_code == 202
    assert not env[1].settle_outbox
    assert first.write_once()
    assert memory_commit_outcomes(caplog) == ['committed']
    assert second.write_once()
    expected = ['committed', 'conflict' if different else 'duplicate']
    assert memory_commit_outcomes(caplog) == expected
    assert not first.index and not second.index
    assert len(env[1].settle_outbox) == 1
    durable = env[1].settle_outbox[(body['terminal']['authorization_id'], 'settle')]
    assert durable['payload_hash'] == row_for(env, body).payload_hash
    assert durable['actual_cost_micro'] == 2
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 0  # enqueue never applies money
    assert _drain_async(concurrency=1)['outcomes'] == {'settled_now': 1}
    assert_settled_once(env, body)
    assert not first.write_once() and not second.write_once()
    assert memory_commit_outcomes(caplog) == expected


def test_evicted_acceptance_then_different_ram_payload(env, ram, caplog):
    body, writer, _ = ram
    alternate = different_valid_payload(env, body)
    caplog.set_level(logging.INFO, logger=memory.logger.name)
    assert call(env, body).status_code == 202
    assert writer.write_once() and not writer.index
    accepted = call(env, alternate)
    assert accepted.status_code == 202 and status(accepted) == 'accepted'
    assert writer.write_once() and not writer.index
    assert memory_commit_outcomes(caplog) == ['committed', 'conflict']
    assert len(env[1].settle_outbox) == 1
    durable = env[1].settle_outbox[(body['terminal']['authorization_id'], 'settle')]
    assert durable['payload_hash'] == row_for(env, body).payload_hash
    assert durable['actual_cost_micro'] == 2
    assert _drain_async(concurrency=1)['outcomes'] == {'settled_now': 1}
    assert_settled_once(env, body)
    assert not writer.write_once()
    assert memory_commit_outcomes(caplog) == ['committed', 'conflict']


@pytest.mark.parametrize('different', [False, True])
def test_synchronous_winner_before_memory_writer(env, ram, caplog, different):
    body, writer, _ = ram
    winner = different_valid_payload(env, body) if different else copy.deepcopy(body)
    caplog.set_level(logging.INFO, logger=memory.logger.name)
    assert call(env, body).status_code == 202
    assert call(env, winner, synchronous=True).status_code == 200
    assert writer.write_once() and not writer.index
    assert not env[1].settle_outbox
    assert memory_commit_outcomes(caplog) == ['reservation_not_open']
    assert_settled_once(env, winner)
    assert not writer.write_once()
    assert memory_commit_outcomes(caplog) == ['reservation_not_open']


@pytest.mark.parametrize('retry_reason', ['expired', 'disabled'])
def test_duplicate_survives_writer_eviction(env, ram, monkeypatch, caplog, retry_reason):
    body, writer, _ = ram
    assert call(env, body).status_code == 202
    if retry_reason == 'expired':
        claims = verify_lookup_ticket(body['settlement_ticket'], [env[2].signer.trusted], NOW)
        monkeypatch.setattr(time, 'time', lambda: claims.exp + 1)
    else:
        env[3].async_settle_enabled = False
    original_get = writer.get
    lookups = []
    def get_then_evict(*key):
        existing = original_get(*key)
        lookups.append(existing)
        if existing is not None:
            assert writer.write_once() and not writer.index
        return existing
    monkeypatch.setattr(writer, 'get', get_then_evict)
    caplog.set_level(logging.INFO, logger=memory.logger.name)
    result = call(env, body)
    assert result.status_code == 202 and status(result) == 'duplicate'
    assert len(lookups) == 1
    assert result.body == handler.response(lookups[0], duplicate=True, resolve=False).body
    assert len(env[1].settle_outbox) == 1
    assert memory_commit_outcomes(caplog) == ['committed']


def test_memory_refund_acceptance_and_commit(env, caplog):
    body, auth, key = prepare(env, kind='refund')
    env[3].async_settle_handoff_mode = 'memory'
    writer = memory.MemoryHandoff()
    writer.thread = ParkedThread()
    env[2].memory = writer
    caplog.set_level(logging.INFO, logger=memory.logger.name)
    result = call(env, body)
    assert result.status_code == 202 and status(result) == 'accepted'
    assert writer.get(auth.id, 'refund').actual_cost_micro == 0
    assert not env[1].settle_outbox
    assert writer.write_once() and not writer.index
    assert memory_commit_outcomes(caplog) == ['committed']
    assert env[1].settle_outbox[(auth.id, 'refund')]['actual_cost_micro'] == 0
    assert _drain_async(concurrency=1)['outcomes'] == {'settled_now': 1}
    assert _typed_credit(env[1], 'ws-v1')['reserved'] == 0
    assert _typed_credit(env[1], 'ws-v1')['total_usage'] == 0
    assert _typed_key(env[1], key.hash)['usage'] == 0


def test_setting_strict(monkeypatch):
    monkeypatch.delenv('TR_ASYNC_SETTLE_HANDOFF_MODE', raising=False)
    assert Settings(environment='test').async_settle_handoff_mode == 'outbox'
    for value in ('memory', 'outbox'):
        monkeypatch.setenv('TR_ASYNC_SETTLE_HANDOFF_MODE', value)
        assert Settings(environment='test').async_settle_handoff_mode == value
    for value in ('', 'Memory', 'memory ', 'invalid'):
        monkeypatch.setenv('TR_ASYNC_SETTLE_HANDOFF_MODE', value)
        with pytest.raises(ValidationError):
            Settings(environment='test')


def test_accept_then_background_commit_and_wire(env, ram, caplog):
    body, writer, clock = ram
    row = row_for(env, body)
    before = copy.deepcopy(env[1].typed)
    with caplog.at_level(logging.INFO):
        accepted = call(env, body)
        assert accepted.status_code == 202
        assert accepted.body == handler.response(row).body
        assert not env[1].settle_outbox
        assert writer.pending('ws-v1') == row.actual_cost_micro
        clock.value += .003
        assert writer.write_once()
    assert not writer.index
    assert env[1].typed == before
    assert len(env[1].settle_outbox) == 1
    assert 'outcome=committed' in caplog.text
    env[3].async_settle_handoff_mode = 'outbox'
    # A new outbox success uses exactly the same row and response bytes.
    env[1].settle_outbox.clear()
    assert call(env, body).body == accepted.body


def test_no_spanner_on_memory_branches(env, ram, monkeypatch):
    body, writer, clock = ram
    monkeypatch.setattr(handler, 'STORE', ForbiddenStore())
    monkeypatch.setattr(handler, 'spanner_settle_outbox', forbidden)
    monkeypatch.setattr(handler, 'enqueue', forbidden)
    env[2].admission.read = forbidden
    env[2].admission.health_read = forbidden
    assert status(call(env, body)) == 'accepted'
    assert status(call(env, body)) == 'duplicate'
    entry = next(iter(writer.index.values()))
    entry.row.payload_hash = 'different'
    with pytest.raises(HTTPException) as exc:
        call(env, body)
    assert exc.value.status_code == 409
    writer.index.clear()
    monkeypatch.setattr(memory, 'ITEM_CAP', 0)
    assert reason(call(env, body)) == 'handoff_queue_full'
    monkeypatch.setattr(memory, 'ITEM_CAP', 64)
    writer.thread.alive = False
    assert reason(call(env, body)) == 'handoff_unhealthy'
    writer.thread.alive = True
    env[2].admission.entries.clear()
    assert reason(call(env, body)) == 'admission_stale'


def test_duplicate_and_conflict(env, ram):
    body, writer, _ = ram
    assert status(call(env, body)) == 'accepted'
    assert status(call(env, body)) == 'duplicate'
    row = row_for(env, body)
    with pytest.raises(HTTPException) as exc:
        writer.accept(replace(row, payload_hash='different'))
    assert exc.value.status_code == 409
    assert len(writer.index) == 1
    assert writer.pending('ws-v1') == row.actual_cost_micro


@pytest.mark.parametrize('limit', ['items', 'amount'])
def test_queue_caps(env, ram, limit):
    body, writer, _ = ram
    row = row_for(env, body)
    count = memory.ITEM_CAP if limit == 'items' else 1
    amount = 0 if limit == 'items' else memory.AMOUNT_CAP_MICRO
    for i in range(count):
        assert writer.accept(replace(row, authorization_id=str(i), actual_cost_micro=amount)).status_code == 202
    assert reason(call(env, body)) == 'handoff_queue_full'
    assert len(writer.index) == count


@pytest.mark.parametrize('unhealthy', ['dead', 'old', 'absent', 'shutdown'])
def test_writer_health(env, ram, unhealthy):
    body, writer, clock = ram
    if unhealthy == 'old':
        writer.accept(replace(row_for(env, body), authorization_id='older'))
        clock.value += memory.MAX_AGE_SECONDS + .001
    elif unhealthy == 'dead':
        writer.thread.alive = False
    elif unhealthy == 'absent':
        env[2].memory = None
    else:
        writer.accepting = False
    assert reason(call(env, body)) == 'handoff_unhealthy'


@pytest.mark.parametrize('check', ['region', 'pilot', 'eligible', 'expired', 'disabled', 'health', 'signature', 'snapshot', 'cohort', 'amount'])
def test_checks_before_acceptance(env, ram, check):
    body, writer, _ = ram
    cfg = env[3]
    if check == 'region':
        cfg._async_settle_accept_region_ids = frozenset({'other'})
    elif check == 'pilot':
        cfg._async_settle_pilot_workspace_ids = frozenset({'other'})
    elif check in {'eligible', 'expired'}:
        claims = verify_lookup_ticket(body['settlement_ticket'], [env[2].signer.trusted], NOW).model_dump()
        if check == 'eligible':
            claims['async_eligible'] = False
        else:
            claims.update(iat=NOW-400, exp=NOW-100)
        body['settlement_ticket'] = env[2].signer.sign(claims, claims['iat'])
    elif check == 'disabled':
        cfg.async_settle_enabled = False
    elif check == 'health':
        env[2].admission.health = None
    elif check == 'signature':
        body['settlement_ticket'] = 'invalid'
    elif check == 'snapshot':
        body['billing_snapshot']['candidates'][0]['rates']['input_micro_per_million'] += 1
    elif check == 'cohort':
        body['observed']['tool_cost'] = True
    else:
        body['terminal']['charge_micro'] += 1
    try:
        assert status(call(env, body)) == 'sync_required'
    except HTTPException as exc:
        assert check in {'signature', 'snapshot', 'amount'}
        assert exc.status_code in {400, 401, 409}
    assert not writer.index
    assert not env[1].settle_outbox


def test_queued_workspace_amount_counts_in_admission(env, ram):
    body, writer, _ = ram
    cfg, cache = env[3], env[2].admission
    cfg.async_settle_pilot_cap_micro = 10
    cache.entries['ws-v1'] = (cache.clock(), Admission(9, 2))
    row = row_for(env, body)
    assert writer.accept(replace(row, authorization_id='other', workspace_id='another')).status_code == 202
    assert call(env, body).status_code == 202
    # 9 durable + 2 local exceeds 10; a fresh identity must be refused.
    body2, _, _ = prepare(env)
    assert reason(call(env, body2)) == 'cap_exceeded'
    assert len(writer.index) == 2


@pytest.mark.parametrize('outcome', ['committed', 'duplicate', 'conflict', 'reservation_not_open'])
def test_background_outcomes(env, ram, monkeypatch, caplog, outcome):
    body, writer, _ = ram
    row = row_for(env, body)
    existing = replace(row, payload_hash='other') if outcome == 'conflict' else row
    writer.outbox = lambda: SimpleNamespace(get=lambda *args: existing)
    def enqueue(outbox, queued, deadline):
        if outcome in {'duplicate', 'conflict'}:
            raise AlreadyExists('private content must not be logged')
        if outcome == 'reservation_not_open':
            raise ReservationNotOpen('private content must not be logged')
    monkeypatch.setattr(handler, 'enqueue', enqueue)
    assert call(env, body).status_code == 202
    with caplog.at_level(logging.INFO):
        assert writer.write_once()
    assert not writer.index and not writer.lost
    assert f'outcome={outcome}' in caplog.text
    assert 'private content' not in caplog.text
    assert row.authorization_id not in caplog.text


# CI run 42372491661 failed this test with the fake clock starting at
# 255.701148817. At the second attempt (start + 0.1),
# (now + ATTEMPT_SECONDS) - now rounds to 5.000000000000028, so checking the
# bound by subtraction failed the assertion inside the fake enqueue, which
# write_once records as a retry, and the entry stayed queued. The bound is
# now checked by addition, the same arithmetic the writer uses for the
# deadline. A start far behind the real monotonic clock (1.0) also pins the
# shared timebase (SharedClockTime).
@pytest.mark.parametrize('start', [None, 255.701148817, 1.0])
@pytest.mark.parametrize('recover', [True, False])
def test_transient_retry_or_budget_loss(env, ram, monkeypatch, caplog, recover, start):
    body, writer, clock = ram
    if start is not None:
        clock.value = start
    calls = []
    writer.outbox = lambda: SimpleNamespace(get=lambda *args: None)
    def enqueue(outbox, row, deadline):
        calls.append((copy.deepcopy(row), deadline))
        assert clock() < deadline <= clock() + memory.ATTEMPT_SECONDS
        if len(calls) == 1 or not recover:
            raise DeadlineExceeded('secret')
    monkeypatch.setattr(handler, 'enqueue', enqueue)
    assert call(env, body).status_code == 202
    # The writer's RPC deadlines are on the fake clock from here on.
    monkeypatch.setattr(storage_gcp_io, 'time', SharedClockTime(clock))
    with caplog.at_level(logging.INFO):
        assert writer.write_once()
        assert len(writer.index) == 1
        assert not writer.write_once()  # backoff, no busy retry
        clock.value += .1
        assert writer.write_once()
        if not recover:
            clock.value += memory.RETRY_SECONDS
            assert writer.write_once()
    assert not writer.index
    assert writer.lost == (0 if recover else 1)
    assert calls[0][0] == calls[1][0]
    assert f"outcome={'committed' if recover else 'lost'}" in caplog.text
    assert 'secret' not in caplog.text


def test_unknown_already_exists_retries(env, ram, monkeypatch):
    body, writer, _ = ram
    writer.outbox = lambda: SimpleNamespace(get=lambda *args: None)
    monkeypatch.setattr(handler, 'enqueue', lambda *args: (_ for _ in ()).throw(AlreadyExists('unknown')))
    call(env, body)
    writer.write_once()
    assert len(writer.index) == 1


def test_shutdown_flushes_in_order_with_fresh_context(env, ram, monkeypatch, caplog):
    body, _, _ = ram
    context = ContextVar('request-content', default=None)
    token = context.set('secret')
    writer = memory.MemoryHandoff(outbox=lambda: SimpleNamespace())
    row = row_for(env, body)
    seen = []
    flush = threading.Event()
    original_join = writer.thread.join
    def join(timeout=None):
        flush.set()
        original_join(timeout)
    monkeypatch.setattr(writer.thread, "join", join)
    def enqueue(outbox, queued, deadline):
        assert flush.wait(1)
        assert context.get() is None
        seen.append(queued.authorization_id)
    monkeypatch.setattr(handler, 'enqueue', enqueue)
    # Hold startup until both entries are queued; no scheduling race in the test.
    with writer.condition:
        writer.thread.start()
        writer.accept(row)
        writer.accept(replace(row, authorization_id='second'))
    try:
        with caplog.at_level(logging.INFO):
            writer.close()
        assert seen == [row.authorization_id, 'second']
    finally:
        flush.set()
        original_join(2)
        context.reset(token)
    assert not writer.index and not writer.lost
    assert not writer.thread.is_alive()
    assert reason(writer.accept(replace(row, authorization_id='third'))) == 'handoff_unhealthy'


def test_shutdown_bound_logs_remaining_once(env, ram, monkeypatch, caplog):
    body, _, _ = ram
    entered, release = threading.Event(), threading.Event()
    def enqueue(*args):
        entered.set()
        assert release.wait(2)
    monkeypatch.setattr(handler, 'enqueue', enqueue)
    monkeypatch.setattr(memory, 'SHUTDOWN_SECONDS', .02)
    writer = memory.MemoryHandoff(outbox=lambda: SimpleNamespace())
    with writer.condition:
        writer.thread.start()
        writer.accept(row_for(env, body))
    assert entered.wait(1)
    start = time.monotonic()
    try:
        with caplog.at_level(logging.INFO):
            writer.close()
            assert time.monotonic() - start < .5
            assert writer.lost == 1 and not writer.index
            release.set()
            writer.thread.join(1)
        assert caplog.text.count('outcome=lost') == 1
        assert 'outcome=committed' not in caplog.text
    finally:
        release.set()
        writer.thread.join(1)


def test_memory_timing_fields(env, ram, caplog):
    body, _, _ = ram
    with caplog.at_level(logging.INFO, logger='trusted_router.async_settle_timing'):
        assert call(env, body).status_code == 202
    line = next(r.message for r in caplog.records if 'metric=async_settle_handoff_phases' in r.message)
    fields = dict(part.split('=') for part in line.split())
    assert fields['outcome'] == 'accepted'
    assert fields['callback_ms'] == fields['commit_ms'] == '0.000'
    assert fields['attempts'] == '0'
    assert float(fields['enqueue_ms']) >= 0
    from trusted_router.async_settle_timing import FIELDS, TOP_LEVEL
    assert list(fields)[2:] == list(FIELDS)
    assert abs(float(fields['total_ms']) - sum(float(fields[k]) for k in (*TOP_LEVEL, 'unclassified_ms'))) < .01


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,surface,enabled,installed', [
    ('memory', 'combined', True, True), ('memory', 'internal', True, True),
    ('outbox', 'combined', True, False), ('memory', 'settle-drain', True, False),
    ('memory', 'combined', False, False),
])
async def test_lifecycle(env, mode, surface, enabled, installed):
    app = FastAPI()
    app.state.async_settle = env[2]
    cfg = settings(async_settle_handoff_mode=mode, async_settle_enabled=enabled)
    cfg.service_surface = surface
    install(app, cfg)
    for start in app.router.on_startup:
        await start()
    worker = env[2].memory
    try:
        assert (worker is not None) == installed
        if worker:
            assert worker.thread.is_alive() and worker.thread.daemon
    finally:
        for stop in app.router.on_shutdown:
            await stop()
    if worker:
        assert not worker.thread.is_alive()
        for start in app.router.on_startup:
            await start()
        restarted = env[2].memory
        try:
            assert restarted is not worker and restarted.thread.is_alive()
        finally:
            for stop in app.router.on_shutdown:
                await stop()
        assert not restarted.thread.is_alive()


def test_synchronous_fallback_unchanged(env, ram, monkeypatch):
    body, writer, _ = ram
    monkeypatch.setattr(writer, 'accept', forbidden)
    result = call(env, body, synchronous=True)
    assert result.status_code == 200
    assert not writer.index
    assert json.loads(result.body)['data']['trusted_router_settlement']['settlement_status'] == 'settled'


def test_unattempted_budget_loss_records_queue_wait(env, ram, monkeypatch, caplog):
    body, writer, clock = ram
    monkeypatch.setattr(handler, 'enqueue', forbidden)
    assert call(env, body).status_code == 202
    clock.value += memory.RETRY_SECONDS
    with caplog.at_level(logging.INFO):
        assert writer.write_once()
    assert writer.lost == 1 and not writer.index
    assert 'queue_wait_ms=120000.000 enqueue_ms=0.000 attempts=0 outcome=lost' in caplog.text
