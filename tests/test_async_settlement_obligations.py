"""Durable registry, inverse MF2 interlock and dormant-path regressions."""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, replace
from datetime import timedelta
from typing import Any

import pytest

from tests.fakes.spanner import FakeSpannerDatabase, _ParamTypes, make_fake_store
from tests.test_settle_outbox_guard import _NOW, _expired_authorization
from tests.test_stage_d_heartbeat import NOW, _heartbeat, _seed, _seed_reaper_counters
from trusted_router.config import Settings
from trusted_router.settlement_journal import (
    Conflict,
    Envelope,
    EpochRegistry,
    Grant,
    Journal,
    Receipt,
    ReceiptVerifier,
    Sizing,
    Ticket,
)
from trusted_router.settlement_journal_memory import InMemoryJournalStorage
from trusted_router.storage_gcp_async_settlement import SpannerEpochRegistry, SpannerReceiptVerifier
from trusted_router.storage_gcp_authorize import (
    SettleOutcome,
    _finalize_reaped_reservation_atomic,
    reap_expired_reservations,
    reap_expired_reservations_result,
    settle_atomic,
)
from trusted_router.storage_gcp_counter_dml import complete_reservation_retention
from trusted_router.storage_gcp_request_records import complete_gateway_authorization_retention

SETTINGS = Settings(async_settlement_journal_minimum_shard_cap_micros=1)


def setup(db: Any = None, *, cap: int = 5_000_000, shards: int = 1, count: int = 1) -> tuple[Any, ...]:
    db = db or FakeSpannerDatabase()
    storage = InMemoryJournalStorage()
    registry = SpannerEpochRegistry(db, _ParamTypes, journal_storage=storage)
    verifier = SpannerReceiptVerifier(registry)
    # Static protocol conformance is checked by mypy as well as runtime use.
    port: EpochRegistry = registry
    receipts: ReceiptVerifier = verifier
    journal = Journal(storage, port, receipts)
    grant = Grant('workspace', 'us-central1', 'epoch', cap, shards, 64)
    registry.register(grant, SETTINGS, tier=1)
    for shard in range(shards):
        journal.run(journal.initialize(grant, shard))
    tickets = []
    for i in range(count):
        ticket = Ticket(grant, i % shards, i // shards, f'auth-{i}', f'gen-{i}', 'key', 'nonce', 'a'*64, 0)
        registry.bind(ticket)
        journal.run(journal.create(ticket))
        tickets.append(ticket)
    return journal, storage, registry, grant, tickets


def finalize_fixture(registry: SpannerEpochRegistry, ticket: Ticket, receipt: Receipt, *, kind: str = 'ledger_finalization') -> None:
    """PR 7 ledger-transaction fixture, not a production receipt minting API."""
    registry.reconcile(ticket)
    def txn(tx: Any) -> None:
        row = registry._obligations(tx, aid=ticket.authorization)[0]
        row['ledger_receipt'] = json.dumps(dict(kind=kind, ticket=asdict(ticket), receipt=asdict(receipt), amount=row['amount']), sort_keys=True, separators=(',', ':'))
        registry._write(tx, 'tr_async_settlement_obligation', row)
    registry.database.run_in_transaction(txn)


def test_registry_lifecycle_receipts_and_restart() -> None:
    j, storage, reg, grant, (ticket,) = setup()
    j.run(j.accept(ticket, Envelope('settle', 'endpoint', 77)))
    proof = Receipt(ticket.authorization, grant.epoch, json.loads(storage.rows[grant.row_key(0)][ticket.column])['hash'], 'ledger')
    assert not j.receipts.verify(ticket, proof)
    finalize_fixture(reg, ticket, proof, kind='import')
    assert not j.receipts.verify(ticket, proof)
    finalize_fixture(reg, ticket, proof)
    assert j.receipts.verify(ticket, proof)
    assert not j.receipts.verify(replace(ticket, nonce='different'), proof)
    assert not j.receipts.verify(ticket, replace(proof, payload_hash='wrong'))
    j.run(j.acknowledge(ticket, proof))
    reg.reconcile(ticket)
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    j.run(j.bound_epoch(grant))
    reg.close(grant, retain_until=20)
    reg = SpannerEpochRegistry(reg.database, _ParamTypes, journal_storage=storage)
    assert reg.grants() == (grant,)
    assert reg.tickets(grant) == (ticket,)
    assert reg.registered(ticket)
    assert reg.is_retired(grant)
    assert reg.retention_deadline(grant) == 20
    reg.close(grant, retain_until=10)
    assert reg.retention_deadline(grant) == 20
    assert reg.allocate(grant, authorization=ticket.authorization, generation=ticket.generation,
                        key_id=ticket.key_id, nonce=ticket.nonce, snapshot_hash=ticket.snapshot_hash,
                        idempotency_until=ticket.idempotency_until) == ticket


@pytest.mark.parametrize('via_journal', [False, True])
@pytest.mark.parametrize('abort_close', [False, True])
def test_close_projects_ack_after_crash_before_reconcile(
    via_journal: bool, abort_close: bool, monkeypatch: Any,
) -> None:
    j, storage, reg, grant, tickets = setup(count=2)
    for ticket in tickets:
        j.run(j.accept(ticket, Envelope('settle', 'endpoint', 77)))
        state = json.loads(storage.rows[grant.row_key(ticket.shard)][ticket.column])
        proof = Receipt(ticket.authorization, grant.epoch, state['hash'], 'ledger')
        finalize_fixture(reg, ticket, proof)
        j.run(j.acknowledge(ticket, proof))
    db = reg.database
    assert {r['state'] for r in db.typed['tr_async_settlement_obligation'].values()} == {'accepted'}
    # Crash before post-ack reconcile: discard the registry/journal objects,
    # retaining only their durable stores. No reconcile is called after restart.
    reg = SpannerEpochRegistry(db, _ParamTypes, journal_storage=storage)
    j = Journal(storage, reg, SpannerReceiptVerifier(reg))
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    for ticket in tickets:
        with pytest.raises(Conflict, match='epoch not closed'):
            j.run(j.purge(ticket, now=20))
        assert ticket.column in storage.rows[grant.row_key(0)]
    def close() -> None:
        if via_journal:
            j.run(j.close_epoch(grant, retain_until=20))
        else:
            reg.close(grant, retain_until=20)
    # The journal must never be read inside the retryable Spanner transaction.
    original_transaction = db.run_in_transaction
    original_call = storage.call
    in_transaction = False
    def transaction(fn: Any, **kwargs: Any) -> Any:
        nonlocal in_transaction
        in_transaction = True
        try:
            return original_transaction(fn, **kwargs)
        finally:
            in_transaction = False
    def call(operation: Any) -> Any:
        assert not in_transaction
        return original_call(operation)
    monkeypatch.setattr(db, 'run_in_transaction', transaction)
    monkeypatch.setattr(storage, 'call', call)
    if abort_close:
        before = copy.deepcopy(db.typed)
        put_grant = reg._put_grant
        def crash(tx: Any, row: Any) -> None:
            put_grant(tx, row)
            raise RuntimeError('crash after closure and projection writes')
        with monkeypatch.context() as patch:
            patch.setattr(reg, '_put_grant', crash)
            with pytest.raises(RuntimeError, match='crash after closure'):
                close()
        assert db.typed == before
        assert reg.retention_deadline(grant) is None
        for ticket in tickets:
            with pytest.raises(Conflict, match='epoch not closed'):
                j.run(j.purge(ticket, now=20))
    close()
    assert reg.retention_deadline(grant) == 20
    assert {r['state'] for r in db.typed['tr_async_settlement_obligation'].values()} == {'acknowledged'}
    for ticket in tickets:
        assert reg.database.typed['tr_async_settlement_obligation'][(ticket.authorization,)]['amount'] == 77
        j.run(j.purge(ticket, now=20))
        assert ticket.column not in storage.rows[grant.row_key(0)]
    # Closed replay remains valid after the journal payloads have been purged.
    reg = SpannerEpochRegistry(db, _ParamTypes, journal_storage=storage)
    reg.close(grant, retain_until=30)
    assert reg.retention_deadline(grant) == 30
    assert {r['state'] for r in db.typed['tr_async_settlement_obligation'].values()} == {'acknowledged'}


def test_registry_close_rejects_unacknowledged_slot_with_zero_outstanding() -> None:
    j, storage, reg, grant, (ticket,) = setup()
    j.run(j.fence(ticket))
    reg.reconcile(ticket)
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    row = storage.rows[grant.row_key(0)]
    assert row[b'sealed'] == b'1' and row[b'outstanding'] == b'0'
    assert json.loads(row[ticket.column])['ack'] is False
    assert reg.database.typed['tr_async_settlement_obligation'][(ticket.authorization,)]['state'] == 'fenced'
    before = copy.deepcopy(reg.database.typed)
    # Call the registry directly: Journal.close_epoch's own check cannot mask
    # removal of the registry check. Fenced is terminal, but still not acked.
    with pytest.raises(Conflict, match='unacknowledged slot'):
        reg.close(grant, retain_until=20)
    assert reg.database.typed == before
    assert reg.retention_deadline(grant) is None
    with pytest.raises(Conflict, match='epoch not closed'):
        j.run(j.purge(ticket, now=20))
    assert ticket.column in storage.rows[grant.row_key(0)]


def test_binding_sizing_and_epoch_cannot_be_recycled() -> None:
    j, _, reg, grant, (ticket,) = setup()
    for changed in (replace(grant, shards=2), replace(grant, slots=128), replace(grant, region='other')):
        with pytest.raises(Conflict):
            reg.register(changed, SETTINGS, tier=1)
    reg.retire(grant)
    with pytest.raises(Conflict):
        reg.bind(replace(ticket, authorization='new'))
    j.run(j.seal_shard(grant, 0))
    j.run(j.bound_epoch(grant))
    successor = reg.successor(grant, 'next', SETTINGS, tier=1, sizing=Sizing(1, 64))
    with pytest.raises(Conflict):
        reg.bind(replace(ticket, grant=successor))
    assert reg.successor(grant, 'next', SETTINGS, tier=1, sizing=Sizing(1, 64)) == successor
    with pytest.raises(Conflict):
        reg.successor(grant, 'different', SETTINGS, tier=1, sizing=Sizing(1, 64))


def test_all_open_epoch_bounds_and_close_preconditions() -> None:
    j, _, reg, grant, (ticket,) = setup()
    with pytest.raises(Conflict):
        reg.close(grant, retain_until=0)
    j.run(j.accept(ticket, Envelope('settle', 'endpoint', 1_000_000)))
    second = j.run(j.rotate(grant, 'second', SETTINGS, tier=1, sizing=Sizing(1, 64)))
    assert second.cap == 4_000_000
    j.run(j.initialize(second, 0))
    t2 = replace(ticket, grant=second, authorization='second-auth')
    reg.bind(t2)
    j.run(j.create(t2))
    j.run(j.accept(t2, Envelope('settle', 'endpoint', 2_000_000)))
    third = j.run(j.rotate(second, 'third', SETTINGS, tier=1, sizing=Sizing(1, 64)))
    assert third.cap == 2_000_000
    with pytest.raises(Conflict):
        reg.close(grant, retain_until=999)
    assert reg.retention_deadline(grant) is None


class CrashProxy:
    def __init__(self, inner: Any, crash_at: int) -> None:
        self.inner, self.crash_at, self.calls = inner, crash_at, 0

    def __getattr__(self, name: str) -> Any:
        target = getattr(self.inner, name)
        if name not in ('execute_sql', 'insert_or_update'):
            return target
        def call(*args: Any, **kwargs: Any) -> Any:
            value = target(*args, **kwargs)
            self.calls += 1
            if self.calls == self.crash_at:
                raise RuntimeError('injected crash')
            return value
        return call


@pytest.mark.parametrize('operation', ['register', 'bind', 'allocate', 'retire', 'bound', 'successor', 'close', 'reconcile'])
@pytest.mark.parametrize('crash_at', range(1, 9))
def test_transaction_statement_crashes(operation: str, crash_at: int, monkeypatch: Any) -> None:
    j, _, reg, grant, (ticket,) = setup()
    db = reg.database
    j.run(j.fence(ticket))
    if operation in ('bound', 'successor', 'close'):
        reg.retire(grant)
        j.run(j.seal_shard(grant, 0))
    if operation == 'successor':
        j.run(j.bound_epoch(grant))
    if operation == 'close':
        from tests.test_settlement_journal import receipt
        proof = receipt(j, ticket)
        finalize_fixture(reg, ticket, proof)
        j.run(j.acknowledge(ticket, proof))
    actions = {
        'register': lambda: reg.register(replace(grant, workspace='other'), SETTINGS, tier=1),
        'bind': lambda: reg.bind(replace(ticket, authorization='another', slot=1)),
        'allocate': lambda: reg.allocate(grant, authorization='another', generation='g', key_id='k', nonce='n', snapshot_hash='b'*64, idempotency_until=0),
        'retire': lambda: reg.retire(grant),
        'bound': lambda: j.run(j.bound_epoch(grant)),
        'successor': lambda: reg.successor(grant, 'next', SETTINGS, tier=1, sizing=Sizing(1, 64)),
        'close': lambda: reg.close(grant, retain_until=10),
        'reconcile': lambda: reg.reconcile(ticket),
    }
    original = db.run_in_transaction
    before = copy.deepcopy(db.typed)
    def crashing(fn: Any, **kwargs: Any) -> Any:
        return original(lambda tx: fn(CrashProxy(tx, crash_at)), **kwargs)
    monkeypatch.setattr(db, 'run_in_transaction', crashing)
    try:
        actions[operation]()
    except RuntimeError as exc:
        assert str(exc) == 'injected crash'
        assert db.typed == before
    monkeypatch.setattr(db, 'run_in_transaction', original)
    actions[operation]()
    after = copy.deepcopy(db.typed)
    # Idempotent replay preserves data, except lifecycle update timestamps.
    actions[operation]()
    for rows in (after, db.typed):
        for table in rows.values():
            for row in table.values():
                row.pop('updated_at', None)
    assert db.typed == after


@pytest.mark.parametrize('seed', range(30))
def test_pr5_three_epoch_random_schedules(seed: int, monkeypatch: Any) -> None:
    from tests import test_settlement_journal as reference
    def durable_setup(**kwargs: Any) -> tuple[Any, ...]:
        result = setup(**kwargs)
        reg = result[2]
        # The old schedule's receipt fixture now writes a real durable row.
        reg.record_ledger_receipt = lambda t, r: finalize_fixture(reg, t, r)
        return result
    monkeypatch.setattr(reference, 'setup', durable_setup)
    reference.test_overlap_random_crashes_preserve_workspace_cap(seed)


@pytest.mark.parametrize('typed', [False, True])
@pytest.mark.parametrize('snapshot', [False, True])
def test_guard_scan_and_transaction_both_reapers(typed: bool, snapshot: bool) -> None:
    if typed:
        db, auth = _seed()
        _seed_reaper_counters(db)
        _heartbeat(db)
        aid, rid, now = auth.id, 'reservation', NOW + timedelta(seconds=301)
    else:
        store, db, _ = make_fake_store()
        auth = _expired_authorization(store, ws='workspace')
        aid, rid, now = auth['authorization_id'], auth['reservation_id'], _NOW
    _, _, reg, grant, _ = setup(db, count=0)
    ticket = Ticket(grant, 0, 0, aid, 'gen', 'key', 'nonce', 'a'*64, 0)
    reg.bind(ticket)
    assert not SETTINGS.async_settlement_journal_enabled
    before = copy.deepcopy(db.reservations)
    assert reap_expired_reservations(db, _ParamTypes, now=now) == 0
    assert reap_expired_reservations_result(db, _ParamTypes, now=now, snapshot_booking_enabled=snapshot).count == 0
    result = _finalize_reaped_reservation_atomic(db, _ParamTypes, reservation_id=rid,
        reap_now=now, guard_outbox=True, snapshot_booking_enabled=snapshot, operational_analytics_outbox=None)
    assert result.outcome == SettleOutcome.OUTBOX_GUARDED
    assert settle_atomic(db, _ParamTypes, reservation_id=rid, actual_micro=0,
        settled_usage_type='Credits', success=False, guard_outbox=True)['outcome'] == SettleOutcome.OUTBOX_GUARDED
    assert db.reservations == before


@pytest.mark.parametrize('typed', [False, True])
def test_reaper_wins_before_binding(typed: bool) -> None:
    if typed:
        db, auth = _seed()
        _seed_reaper_counters(db)
        aid, now = auth.id, NOW + timedelta(seconds=301)
    else:
        store, db, _ = make_fake_store()
        auth = _expired_authorization(store, ws='workspace')
        aid, now = auth['authorization_id'], _NOW
    _, _, reg, grant, _ = setup(db, count=0)
    assert reap_expired_reservations(db, _ParamTypes, now=now) == 1
    with pytest.raises(Conflict):
        reg.bind(Ticket(grant, 0, 0, aid, 'gen', 'key', 'nonce', 'a'*64, 0))
    assert not reg.tickets(grant)


@pytest.mark.parametrize('state', ['pending', 'accepted', 'sync_required', 'fenced', 'acknowledged'])
@pytest.mark.parametrize('outbox', [False, True])
def test_retention_is_guarded_independently_of_flags(state: str, outbox: bool) -> None:
    db, auth = _seed(settled=True)
    db.reservations['reservation']['settled'] = True
    db.gateway_authorizations[auth.id]['settled'] = True
    db.typed.setdefault('tr_async_settlement_obligation', {})[(auth.id,)] = {'state': state}
    def txn(tx: Any) -> tuple[int, int]:
        return (complete_reservation_retention(tx, _ParamTypes, 'reservation', terminal_at=NOW, outbox_available=outbox),
                complete_gateway_authorization_retention(tx, _ParamTypes, auth.id, terminal_at=NOW, outbox_available=outbox))
    allowed = int(state in ('acknowledged', 'fenced'))
    assert db.run_in_transaction(txn) == (allowed, allowed)
    assert (db.gateway_authorizations[auth.id]['terminal_at'] is not None) == bool(allowed)


@pytest.mark.parametrize('typed', [False, True])
@pytest.mark.parametrize('winner', ['guard', 'reaper'])
def test_mf2_commit_barriers(typed: bool, winner: str, monkeypatch: Any) -> None:
    if typed:
        db, auth = _seed()
        _seed_reaper_counters(db)
        _heartbeat(db)
        aid, rid, now = auth.id, 'reservation', NOW + timedelta(seconds=301)
    else:
        store, db, _ = make_fake_store()
        auth = _expired_authorization(store, ws='workspace')
        aid, rid, now = auth['authorization_id'], auth['reservation_id'], _NOW
    _, _, reg, grant, _ = setup(db, count=0)
    ticket = Ticket(grant, 0, 0, aid, 'g', 'k', 'n', 'a'*64, 0)
    original = db._try_commit
    fired = False
    def reap() -> Any:
        if typed:
            return reap_expired_reservations_result(db, _ParamTypes, now=now, snapshot_booking_enabled=True)
        return reap_expired_reservations(db, _ParamTypes, now=now)
    def barrier(tx: Any) -> bool:
        nonlocal fired
        # Pause just after the loser's reads/writes but before commit. Commit
        # the other transaction, then resume: the fake must detect the stale
        # absence/read and retry, exactly the Spanner serialization interlock.
        if not fired and tx.pending_writes:
            fired = True
            if winner == 'guard':
                reg.bind(ticket)
            else:
                reap()
        return original(tx)
    monkeypatch.setattr(db, '_try_commit', barrier)
    if winner == 'guard':
        reap()
        assert reg.registered(ticket)
        assert not db.reservations[rid]['settled']
    else:
        with pytest.raises(Conflict):
            reg.bind(ticket)
        assert db.reservations[rid]['settled']
        assert not reg.registered(ticket)
    assert fired and db.aborts >= 1


def test_guarded_scan_cannot_starve_later_holds() -> None:
    store, db, _ = make_fake_store()
    guarded = _expired_authorization(store, ws='workspace')
    later = _expired_authorization(store, ws='later')
    _, _, reg, grant, _ = setup(db, count=0)
    reg.bind(Ticket(grant, 0, 0, guarded['authorization_id'], 'g', 'k', 'n', 'a'*64, 0))
    assert reap_expired_reservations(db, _ParamTypes, now=_NOW, limit=1) == 1
    assert not db.reservations[guarded['reservation_id']]['settled']
    assert db.reservations[later['reservation_id']]['settled']


@pytest.mark.parametrize("obligation_present", [False, True])
def test_dormant_parent_byte_capture(obligation_present: bool) -> None:
    import hashlib

    from tests.async_settlement_dormancy import capture
    # Parent HEAD capture includes all returned fields and durable row payloads
    # in eight cohort/heartbeat/snapshot combinations; no normalization of data.
    assert hashlib.sha256(capture(obligation_present=obligation_present).encode()).hexdigest() == "c4ea6650a4aa64dff23ec2a4025010ca82f1c2347534b76e99acfb35fac04bfe"


def test_outbox_completion_cannot_arm_guarded_retention() -> None:
    from tests.test_settle_outbox_guard import _row
    from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox

    db, auth = _seed()
    outbox = SpannerSettleOutbox(db, _ParamTypes)
    outbox.enqueue(_row(auth.id, 'reservation'))
    db.gateway_authorizations[auth.id]['settled'] = True
    db.reservations['reservation']['settled'] = True
    db.typed.setdefault('tr_async_settlement_obligation', {})[(auth.id,)] = {'state': 'accepted'}
    assert outbox.mark(auth.id, 'settle', done=True) == 'done'
    assert db.gateway_authorizations[auth.id]['terminal_at'] is None
    assert db.reservations['reservation']['terminal_at'] is None


def test_receipt_and_reconciliation_reject_cross_epoch_binding() -> None:
    j, storage, reg, grant, (ticket,) = setup()
    j.run(j.fence(ticket))
    successor = j.run(j.rotate(grant, 'next', SETTINGS, tier=1, sizing=Sizing(1, 64)))
    # Simulate an inconsistent journal record: recovery may never apply it to
    # another epoch's authorization tombstone even if all other fields match.
    wrong = replace(ticket, grant=successor)
    j.run(j.initialize(successor, 0))
    state = json.loads(storage.rows[grant.row_key(0)][ticket.column])
    state['ticket'] = asdict(wrong)
    storage.rows[successor.row_key(0)][wrong.column] = json.dumps(state).encode()
    with pytest.raises(Conflict):
        reg.reconcile(wrong)
    assert not reg.registered(wrong)


def test_concurrent_workspace_registration_serializes() -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    db = FakeSpannerDatabase(ready_barrier=Barrier(2))
    registry = SpannerEpochRegistry(db, _ParamTypes)
    def register(epoch: str) -> bool:
        try:
            registry.register(Grant('workspace', 'us-central1', epoch, 5_000_000, 1, 64), SETTINGS, tier=1)
            return True
        except Conflict:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(register, ['one', 'two'])) == [False, True]
    assert len(registry.grants()) == 1


@pytest.mark.parametrize("obligation_present", [False, True])
def test_dormant_legacy_parent_byte_capture(obligation_present: bool) -> None:
    import hashlib

    from tests.async_settlement_dormancy import capture_legacy

    assert hashlib.sha256(capture_legacy(obligation_present=obligation_present).encode()).hexdigest() == "78873e0382ad4659e03c002c21141c5f99a6510b38305c7914c39db5738cec8c"


@pytest.mark.parametrize('outbox_present', [False, True])
@pytest.mark.parametrize('mode', ['settle', 'typed', 'speculative', 'outbox'])
@pytest.mark.parametrize('obligation_present', [False, True])
def test_pre_migration_settlement(outbox_present: bool, mode: str, obligation_present: bool) -> None:
    from tests.test_settle_outbox_guard import _row
    from trusted_router.storage_gcp_authorize import typed_finalize_atomic
    from trusted_router.storage_gcp_settle_outbox import SpannerSettleOutbox

    db, auth = _seed()
    _seed_reaper_counters(db)
    if not obligation_present:
        db.missing_tables.add('tr_async_settlement_obligation')
    if not outbox_present:
        db.missing_tables.add('tr_settle_outbox')
    if mode == 'outbox':
        if not outbox_present:
            return  # An absent outbox has no intents to complete.
        outbox = SpannerSettleOutbox(db, _ParamTypes)
        outbox.enqueue(_row(auth.id, 'reservation'))
        db.gateway_authorizations[auth.id]['settled'] = True
        db.reservations['reservation']['settled'] = True
        assert outbox.mark(auth.id, 'settle', done=True) == 'done'
        assert db.gateway_authorizations[auth.id]['terminal_at'] is not None
    elif mode == 'settle':
        result = settle_atomic(db, _ParamTypes, reservation_id='reservation',
            actual_micro=17, settled_usage_type='Credits', success=True)
        assert result['outcome'] == SettleOutcome.SETTLED
    else:
        if mode == 'typed':
            from trusted_router.storage_gcp_counter_dml import insert_entity_dml_at
            db.run_in_transaction(lambda tx: insert_entity_dml_at(
                tx, _ParamTypes, 'gateway_authorization', auth.id, '{}', NOW))
        if outbox_present:
            SpannerSettleOutbox(db, _ParamTypes).enqueue(_row(auth.id, 'reservation'))
        result = typed_finalize_atomic(db, _ParamTypes, reservation_id='reservation',
            authorization_id=auth.id, success=True, actual_micro=17,
            settled_usage_type='Credits', now=NOW,
            auth_body_settled=json.dumps({'id': auth.id, 'settled': True}),
            authorization=replace(auth, settled=True) if mode == 'speculative' else None,
            settle_outbox_done=(auth.id, 'settle') if outbox_present else None)
        assert result['outcome'] == SettleOutcome.SETTLED
    # Parent typed speculation defers TTL until outbox completion.
    assert (db.reservations['reservation']['terminal_at'] is not None) == (mode != 'speculative' or outbox_present)
    assert db.reservations['reservation']['settled'] is True


@pytest.mark.parametrize('outbox_present', [False, True])
@pytest.mark.parametrize('obligation_present', [False, True])
def test_table_availability_independent(outbox_present: bool, obligation_present: bool) -> None:
    from trusted_router.storage_gcp_async_settlement import obligation_table_available
    from trusted_router.storage_gcp_authorize import _outbox_table_available

    db, auth = _seed()
    _seed_reaper_counters(db)
    if not outbox_present:
        db.missing_tables.add('tr_settle_outbox')
    if not obligation_present:
        db.missing_tables.add('tr_async_settlement_obligation')
    else:
        db.typed.setdefault('tr_async_settlement_obligation', {})[(auth.id,)] = {'state': 'accepted'}
    assert _outbox_table_available(db, _ParamTypes) is outbox_present
    assert obligation_table_available(db, _ParamTypes) is obligation_present
    now = NOW + timedelta(seconds=301)
    # Exercise both transactional entry points without using the advisory scan.
    result = _finalize_reaped_reservation_atomic(db, _ParamTypes, reservation_id='reservation',
        reap_now=now, guard_outbox=outbox_present, snapshot_booking_enabled=True,
        operational_analytics_outbox=None)
    assert result.outcome == (SettleOutcome.OUTBOX_GUARDED if obligation_present else SettleOutcome.SETTLED)
    if obligation_present:
        result = settle_atomic(db, _ParamTypes, reservation_id='reservation', actual_micro=0,
            settled_usage_type='Credits', success=False, guard_outbox=outbox_present,
            outbox_available=outbox_present, expires_before=now)
        assert result['outcome'] == SettleOutcome.OUTBOX_GUARDED


def test_obligation_availability_transition(monkeypatch: Any) -> None:
    from trusted_router import storage_gcp_async_settlement as adapter

    db, auth = _seed()
    _seed_reaper_counters(db)
    # Named production-shaped client. Cache state is test-local.
    monkeypatch.setattr(adapter, '_OBLIGATION_AVAILABILITY_CACHE', {})
    db.name = 'projects/test/instances/test/databases/transition'
    db.missing_tables.add('tr_async_settlement_obligation')
    clock = [100.0]
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: clock[0])
    assert adapter.obligation_table_available(db, _ParamTypes) is False
    reads = db.snapshot_execute_sql_calls
    db.missing_tables.clear()
    assert adapter.obligation_table_available(db, _ParamTypes) is False
    assert db.snapshot_execute_sql_calls == reads
    clock[0] += adapter.OBLIGATION_ABSENT_CACHE_SECONDS
    assert adapter.obligation_table_available(db, _ParamTypes) is True
    assert db.snapshot_execute_sql_calls == reads + 1
    clock[0] += 100
    assert adapter.obligation_table_available(db, _ParamTypes) is True
    assert db.snapshot_execute_sql_calls == reads + 1
    db.typed.setdefault('tr_async_settlement_obligation', {})[(auth.id,)] = {'state': 'accepted'}
    assert reap_expired_reservations(db, _ParamTypes, now=NOW+timedelta(seconds=301)) == 0
    assert not db.reservations['reservation']['settled']


def test_flag_on_registry_missing_obligation_raises() -> None:
    from google.api_core.exceptions import NotFound

    db = FakeSpannerDatabase()
    db.missing_tables.add('tr_async_settlement_obligation')
    registry = SpannerEpochRegistry(db, _ParamTypes)
    settings = Settings(async_settlement_journal_enabled=True, bigtable_instance_id="test",
                        async_settlement_journal_bigtable_app_profiles="region=profile",
                        async_settlement_journal_minimum_shard_cap_micros=1)
    with pytest.raises(NotFound, match='Table not found: tr_async_settlement_obligation'):
        registry.register(Grant('workspace', 'region', 'epoch', 5_000_000, 1, 64), settings, tier=1)
    assert not db.typed


@pytest.mark.parametrize('message', [
    'Session not found while querying tr_async_settlement_obligation',
    'column state of tr_async_settlement_obligation does not exist',
    'Table not found: tr_settle_outbox',
])
def test_obligation_probe_other_errors_keep_guards(message: str, monkeypatch: Any) -> None:
    from google.api_core.exceptions import NotFound

    from trusted_router import storage_gcp_async_settlement as adapter

    db = FakeSpannerDatabase()
    db.name = 'projects/test/instances/test/databases/errors'
    monkeypatch.setattr(adapter, '_OBLIGATION_AVAILABILITY_CACHE', {})
    def fail() -> Any:
        raise NotFound(message)
    monkeypatch.setattr(db, 'snapshot', fail)
    assert adapter.obligation_table_available(db, _ParamTypes) is True
    assert not adapter._OBLIGATION_AVAILABILITY_CACHE


@pytest.mark.parametrize('obligation_present', [False, True])
def test_dormant_settlement_parent_byte_capture(obligation_present: bool) -> None:
    import hashlib

    from tests.async_settlement_dormancy import capture_settlement

    assert hashlib.sha256(capture_settlement(obligation_present=obligation_present).encode()).hexdigest() == "b13efc8fad36629d1394a0e642e18b8c38547bb3f4fa1ef80308bb12e47ef8ac"
