"""Same production transitions, interleaved at real row-RPC boundaries."""
from __future__ import annotations

import itertools
import json
import random
from dataclasses import replace
from typing import Any

import pytest

from trusted_router.config import Settings
from trusted_router.settlement_journal import (
    META,
    Compare,
    Conflict,
    Envelope,
    Grant,
    InMemoryEpochRegistry,
    Journal,
    JournalError,
    Operation,
    Read,
    Receipt,
    RetryLater,
    Ticket,
    encode,
)
from trusted_router.settlement_journal_memory import InMemoryJournalStorage


def setup(*, cap: int = 100, shards: int = 1, count: int = 3) -> tuple[Any, ...]:
    registry = InMemoryEpochRegistry()
    grant = Grant('workspace', 'us-central1', 'epoch', cap, shards, 64)
    registry.register(grant, Settings(async_settlement_journal_minimum_shard_cap_micros=1), tier=1)
    storage = InMemoryJournalStorage()
    journal = Journal(storage, registry, registry)
    tickets = [Ticket(grant, i % shards, i // shards, f'auth-{i}', f'gen-{i}',
                      'key', 'nonce', 'a' * 64, 0) for i in range(count)]
    for shard in range(shards):
        journal.run(journal.initialize(grant, shard))
    for ticket in tickets:
        registry.bind(ticket)
        journal.run(journal.create(ticket))
    return journal, storage, registry, grant, tickets


def receipt(journal: Journal, ticket: Ticket) -> Receipt:
    row = journal.storage.call(Read(ticket.grant.row_key(ticket.shard), (*META, ticket.column)))
    state = json.loads(row[ticket.column])
    return Receipt(ticket.authorization, ticket.grant.epoch, state.get('hash', ''), 'ledger-commit')


def acknowledge(j: Journal, reg: InMemoryEpochRegistry, ticket: Ticket) -> None:
    proof = receipt(j, ticket)
    reg.record_ledger_receipt(ticket, proof)
    j.run(j.acknowledge(ticket, proof))


def invariant(storage: InMemoryJournalStorage, grant: Grant,
              history: dict[bytes, bytes] | None = None) -> None:
    total = 0
    for shard in range(grant.shards):
        row = storage.rows[grant.row_key(shard)]
        accepted = 0
        assert len(row) <= grant.slots + len(META)
        for column, raw in row.items():
            if not column.startswith(b's/'):
                continue
            state = json.loads(raw)
            if state['status'] == 'accepted':
                if not state['ack']:
                    accepted += state['envelope']['charge']
                immutable = encode({k: state[k] for k in ('ticket', 'envelope', 'hash', 'status')})
                if history is not None:
                    identity = grant.row_key(shard) + column
                    assert history.setdefault(identity, immutable) == immutable
            if history is not None and grant.row_key(shard) + column in history:
                assert state['status'] == 'accepted'  # Refund/fence cannot overwrite.
        assert 0 <= accepted == int(row[b'outstanding']) <= int(row[b'cap'])
        total += accepted
    assert total <= grant.cap


def test_accept_retry_conflict_ack_and_cap() -> None:
    j, storage, reg, grant, tickets = setup()
    ticket, second, _ = tickets
    envelope = Envelope('settle', 'endpoint', 75, (('input', 3),))
    before = j.metrics.rpc_calls
    original = j.run(j.accept(ticket, envelope))
    assert j.metrics.rpc_calls - before == 2
    assert original.status == 'accepted'
    assert j.run(j.accept(ticket, envelope)) == original
    for changed in (replace(envelope, charge=74), replace(envelope, endpoint='other'),
                    Envelope('refund', 'endpoint', 0), replace(envelope, usage=(('input', 4),))):
        with pytest.raises(Conflict):
            j.run(j.accept(ticket, changed))
    assert j.run(j.accept(second, Envelope('settle', 'endpoint', 26))).status == 'sync_required'
    assert j.run(j.accept(second, Envelope('settle', 'endpoint', 26))).status == 'sync_required'
    with pytest.raises(Conflict):
        j.run(j.accept(second, Envelope('settle', 'endpoint', 1)))
    invariant(storage, grant)
    acknowledge(j, reg, ticket)
    acknowledge(j, reg, ticket)
    assert j.run(j.accept(ticket, envelope)) == original
    invariant(storage, grant)
    assert storage.rows[grant.row_key(0)][b'outstanding'] == b'0'


def test_fence_refund_and_receipt_binding() -> None:
    j, storage, reg, grant, tickets = setup()
    t = tickets[0]
    outcome = j.run(j.accept(t, Envelope('refund', 'endpoint', 0)))
    assert outcome.status == 'accepted'
    with pytest.raises(Conflict):
        j.run(j.accept(t, Envelope('settle', 'endpoint', 1)))
    assert j.run(j.fence(t)) == outcome
    proof = receipt(j, t)
    with pytest.raises(Conflict):
        j.run(j.acknowledge(t, proof))
    reg.record_ledger_receipt(t, replace(proof, payload_hash='wrong'))
    with pytest.raises(Conflict):
        j.run(j.acknowledge(t, replace(proof, payload_hash='wrong')))
    assert j.run(j.fence(tickets[1])).status == 'fenced'
    assert j.run(j.accept(tickets[1], Envelope('settle', 'endpoint', 1))).status == 'fenced'
    with pytest.raises(Conflict):
        j.run(j.accept(replace(t, nonce='new-nonce'), Envelope('refund', 'endpoint', 0)))
    invariant(storage, grant)


class Task:
    def __init__(self, operation: Operation) -> None:
        self.operation = operation
        self.response: Any = None
        self.done = False
        self.outcome: Any = None

    def step(self, storage: InMemoryJournalStorage) -> None:
        try:
            request = self.operation.send(self.response)
        except StopIteration as stopped:
            self.done, self.outcome = True, stopped.value
            return
        except (Conflict, RetryLater):
            self.done = True
            return
        self.response = storage.call(request)


@pytest.mark.parametrize('schedule', list(itertools.product(range(2), repeat=8)))
def test_exhaustive_accept_fence_schedules(schedule: tuple[int, ...]) -> None:
    j, storage, _, grant, tickets = setup(count=1)
    operations = [Task(j.accept(tickets[0], Envelope('settle', 'endpoint', 99))),
                  Task(j.fence(tickets[0]))]
    history: dict[bytes, bytes] = {}
    for index in (*schedule, *([0, 1] * 8)):
        task = operations[index]
        if not task.done:
            task.step(storage)
            invariant(storage, grant, history)
    assert all(t.done for t in operations)
    state = json.loads(storage.rows[grant.row_key(0)][tickets[0].column])
    assert state['status'] in ('accepted', 'fenced')
    # Both callers observe the same winning terminal choice.
    assert operations[0].outcome == operations[1].outcome


@pytest.mark.parametrize('seed', range(50))
def test_random_schedules_crashes_retries_ack_seal(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - reproducible interleavings
    j, storage, reg, grant, tickets = setup(cap=20, count=6)
    tasks = [Task(j.accept(t, Envelope('settle', 'endpoint', 7))) for t in tickets]
    history: dict[bytes, bytes] = {}
    for step in range(150):
        if step == 70:
            reg.retire(grant)
            tasks.append(Task(j.seal_shard(grant, 0)))
        t = rng.choice(tickets)
        if rng.random() < 0.25:
            tasks.append(Task(j.accept(t, Envelope('settle', 'endpoint', 7))))
        if rng.random() < 0.15:
            tasks.append(Task(j.fence(t)))
        state = json.loads(storage.rows[grant.row_key(0)][t.column])
        if state['status'] != 'pending' and rng.random() < 0.2:
            proof = receipt(j, t)
            reg.record_ledger_receipt(t, proof)
            tasks.append(Task(j.acknowledge(t, proof)))
        active = [task for task in tasks if not task.done]
        if active:
            chosen = rng.choice(active)
            chosen.step(storage)
            # Crash AFTER an RPC applied, including before its response is consumed.
            if rng.random() < 0.1:
                chosen.done = True
        invariant(storage, grant, history)
    # Durable recovery uses registered identities, not notifications/tasks.
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    for t in tickets:
        j.run(j.fence(t))
        acknowledge(j, reg, t)
        acknowledge(j, reg, t)
        invariant(storage, grant, history)
    j.run(j.close_epoch(grant, retain_until=1000))
    for t in tickets:
        with pytest.raises(Conflict):
            j.run(j.purge(t, now=999))
        j.run(j.purge(t, now=1000))
        j.run(j.purge(t, now=1000))
    assert storage.rows[grant.row_key(0)][b'sealed'] == b'2'
    with pytest.raises(Conflict):
        j.run(j.create(tickets[0]))


@pytest.mark.parametrize('operation_name', ['initialize', 'create', 'accept', 'fence', 'ack', 'seal', 'recover', 'close', 'purge'])
@pytest.mark.parametrize('crash_after', range(7))
def test_crash_after_every_operation_rpc(operation_name: str, crash_after: int) -> None:
    j, storage, reg, grant, tickets = setup(count=1)
    t = tickets[0]
    envelope = Envelope('settle', 'endpoint', 10)
    if operation_name in ('create', 'recover'):
        storage.rows[grant.row_key(0)].pop(t.column)
    if operation_name in ('ack', 'close', 'purge'):
        j.run(j.accept(t, envelope))
    if operation_name in ('seal', 'recover', 'close', 'purge'):
        reg.retire(grant)
    proof = receipt(j, t) if operation_name not in ('create', 'recover') else Receipt('a', 'e', '', 'r')
    reg.record_ledger_receipt(t, proof)
    if operation_name in ('close', 'purge'):
        acknowledge(j, reg, t)
        j.run(j.seal_shard(grant, 0))
    if operation_name == 'purge':
        j.run(j.close_epoch(grant, retain_until=1))
    factories = {
        'initialize': lambda: j.initialize(grant, 0), 'create': lambda: j.create(t),
        'accept': lambda: j.accept(t, envelope), 'fence': lambda: j.fence(t),
        'ack': lambda: j.acknowledge(t, proof), 'seal': lambda: j.seal_shard(grant, 0),
        'recover': lambda: j.recover_slot(t),
        'close': lambda: j.close_epoch(grant, retain_until=1),
        'purge': lambda: j.purge(t, now=2),
    }
    task = Task(factories[operation_name]())
    for _ in range(crash_after):
        if not task.done:
            task.step(storage)
            invariant(storage, grant)
    # Lose process and RPC result; restart against the same durable cells.
    j.run(factories[operation_name]())
    invariant(storage, grant)
    if operation_name == 'accept':
        assert j.run(j.accept(t, envelope)).charge == 10


def test_contention_is_bounded_and_slot_fence_is_unconditional_on_version() -> None:
    j, storage, _, grant, tickets = setup()
    original = storage.call

    def conflicts(request: Any) -> Any:
        if isinstance(request, Compare) and request.column == b'version':
            return False
        return original(request)

    storage.call = conflicts
    assert j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 1))).status == 'sync_required'
    assert j.metrics.cas_conflicts == 2
    assert j.metrics.contention_fallbacks == 1
    invariant(storage, grant)


def test_unknown_never_becomes_sync_or_zero() -> None:
    j, storage, _, _, tickets = setup()
    original = storage.call

    def lost_reply(request: Any) -> Any:
        response = original(request)
        if isinstance(request, Compare):
            raise JournalError('lost commit response')
        return response

    storage.call = lost_reply
    with pytest.raises(JournalError):
        j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 10)))
    storage.call = original
    assert j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 10))).status == 'accepted'


def test_retention_never_deletes_pending_or_unacknowledged() -> None:
    j, storage, reg, grant, tickets = setup()
    j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 10)))
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    with pytest.raises(Conflict):
        j.run(j.close_epoch(grant, retain_until=0))
    # Even a bad external registry's retention claim must not delete live debt.
    reg.close(grant, retain_until=0)
    for t in tickets:
        with pytest.raises(Conflict):
            j.run(j.purge(t, now=10**12))
        assert t.column in storage.rows[grant.row_key(0)]


def test_registry_one_region_tier_caps_identity_and_slot_bound() -> None:
    j, _, reg, grant, tickets = setup()
    for tier in (None, 0, 4, True, False):
        with pytest.raises(Conflict):
            InMemoryEpochRegistry().register(grant, Settings(async_settlement_journal_minimum_shard_cap_micros=1), tier=tier)
    for tier, cap in ((1, 5_000_000), (2, 25_000_000), (3, 100_000_000)):
        r = InMemoryEpochRegistry()
        r.register(replace(grant, cap=cap), Settings(async_settlement_journal_minimum_shard_cap_micros=1), tier=tier)
        if tier != 3:
            with pytest.raises(Conflict):
                InMemoryEpochRegistry().register(replace(grant, cap=cap + 1), Settings(async_settlement_journal_minimum_shard_cap_micros=1), tier=tier)
    with pytest.raises(Conflict):
        reg.register(replace(grant, region='europe-west4'), Settings(async_settlement_journal_minimum_shard_cap_micros=1), tier=1)
    with pytest.raises(Conflict):
        reg.bind(replace(tickets[0], nonce='changed'))
    with pytest.raises(Conflict):
        reg.bind(replace(tickets[0], authorization='other'))
    with pytest.raises(ValueError):
        replace(tickets[0], slot=64)
    assert j.run(j.create(tickets[0])).status == 'pending'


def test_config_defaults_and_profile_parser() -> None:
    settings = Settings()
    assert not settings.async_settlement_journal_enabled
    assert settings.async_settlement_journal_bigtable_app_profile_map == {}
    assert settings.async_settlement_journal_rpc_timeout_seconds == 0.25
    settings = Settings(async_settlement_journal_bigtable_app_profiles='us-central1=a,europe-west4=b')
    assert settings.async_settlement_journal_bigtable_app_profile_map == {'us-central1': 'a', 'europe-west4': 'b'}
    for invalid in ('us-central1=a,us-central1=b', 'a', '=a', 'a=', 'a=b=c'):
        with pytest.raises(ValueError):
            _ = Settings(async_settlement_journal_bigtable_app_profiles=invalid).async_settlement_journal_bigtable_app_profile_map


def test_partial_seal_missing_slot_and_resume() -> None:
    j, storage, reg, grant, tickets = setup(cap=200, shards=2, count=2)
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    assert j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 1))).status == 'sync_required'
    assert j.run(j.accept(tickets[1], Envelope('settle', 'endpoint', 1))).status == 'accepted'
    with pytest.raises(Conflict):
        j.run(j.close_epoch(grant, retain_until=0))
    j.run(j.seal_shard(grant, 1))
    for t in tickets:
        acknowledge(j, reg, t)
    j.run(j.close_epoch(grant, retain_until=0))
    invariant(storage, grant)
    # Delayed initialization must never erase sealing or an old terminal choice.
    j.run(j.initialize(grant, 0))
    assert storage.rows[grant.row_key(0)][b'sealed'] == b'1'
    j.run(j.purge(tickets[0], now=1))
    with pytest.raises(JournalError):
        j.run(j.fence(tickets[0]))


def test_two_distinct_accepts_cannot_overrun_shard() -> None:
    j, storage, _, grant, tickets = setup(cap=100, count=2)
    a = j.accept(tickets[0], Envelope('settle', 'endpoint', 70))
    b = j.accept(tickets[1], Envelope('settle', 'endpoint', 70))
    read_a, read_b = next(a), next(b)
    snapshot_a, snapshot_b = storage.call(read_a), storage.call(read_b)
    cas_a, cas_b = a.send(snapshot_a), b.send(snapshot_b)
    assert storage.call(cas_a)
    assert not storage.call(cas_b)
    request = b.send(False)
    response = storage.call(request)
    while True:
        try:
            request = b.send(response)
        except StopIteration as done:
            assert done.value.status == 'sync_required'
            break
        response = storage.call(request)
        invariant(storage, grant)
    assert storage.rows[grant.row_key(0)][b'outstanding'] == b'70'


@pytest.mark.parametrize('concurrency', [10, 50, 150, 200])
def test_hashed_shards_burst_and_rpc_model(concurrency: int) -> None:
    SHARDS = 4096

    registry = InMemoryEpochRegistry()
    grant = Grant('workspace', 'us-central1', 'epoch', 5_000_000, SHARDS)
    registry.register(grant, Settings(async_settlement_journal_minimum_shard_cap_micros=1), tier=1)
    storage = InMemoryJournalStorage()
    journal = Journal(storage, registry, registry)
    tickets = [registry.allocate(grant, authorization=f'load-{i}', generation=f'g-{i}',
                                 key_id='key', nonce='nonce', snapshot_hash='a' * 64, idempotency_until=0)
               for i in range(concurrency)]
    for shard in {t.shard for t in tickets}:
        journal.run(journal.initialize(grant, shard))
    for ticket in tickets:
        journal.run(journal.create(ticket))
    before = journal.metrics.rpc_calls
    operations = [journal.accept(t, Envelope('settle', 'endpoint', 1)) for t in tickets]
    # Worst synchronized read wave: all readers precede all mutations.
    pending = [(op, storage.call(next(op))) for op in operations]
    calls = [1] * concurrency
    outcomes = {}
    while pending:
        next_round = []
        for op, response in pending:
            index = operations.index(op)
            try:
                request = op.send(response)
            except StopIteration as done:
                outcomes[index] = done.value
                continue
            calls[index] += 1
            next_round.append((op, storage.call(request)))
        pending = next_round
    assert all(outcome.status == 'accepted' for outcome in outcomes.values())
    assert sum(c <= 3 for c in calls) >= 0.95 * concurrency
    assert max(calls) <= 4
    assert journal.metrics.rpc_calls == before  # Scheduler, not executor, drove RPCs.
    expected_losers = concurrency - SHARDS * (1 - (1 - 1 / SHARDS)**concurrency)
    assert expected_losers / concurrency < 0.025


def test_duplicate_small_charge_does_not_increment() -> None:
    j, storage, _, grant, tickets = setup()
    envelope = Envelope('settle', 'endpoint', 10)
    first = j.run(j.accept(tickets[0], envelope))
    assert j.run(j.accept(tickets[0], envelope)) == first
    assert storage.rows[grant.row_key(0)][b'outstanding'] == b'10'


def test_bound_but_missing_slot_can_be_fenced_after_retirement() -> None:
    j, storage, reg, grant, tickets = setup(count=1)
    ticket = tickets[0]
    # Crash after registry bind but before ANY Bigtable initialization.
    storage.rows.clear()
    with pytest.raises(JournalError):
        j.run(j.fence(ticket))
    with pytest.raises(Conflict):
        j.run(j.recover_slot(ticket))
    reg.retire(grant)
    outcome = j.run(j.recover_slot(ticket))
    assert outcome.status == 'fenced'
    assert j.run(j.recover_slot(ticket)) == outcome
    assert j.run(j.accept(ticket, Envelope('settle', 'endpoint', 1))) == outcome
    acknowledge(j, reg, ticket)
    j.run(j.close_epoch(grant, retain_until=0))
    invariant(storage, grant)


def test_missing_slot_repair_never_overwrites_an_accepted_winner() -> None:
    j, storage, reg, grant, tickets = setup(count=1)
    expected = j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 70)))
    reg.retire(grant)
    assert j.run(j.recover_slot(tickets[0])) == expected
    invariant(storage, grant)


def test_delayed_missing_slot_repair_cannot_resurrect_after_purge() -> None:
    j, storage, reg, grant, tickets = setup(count=1)
    t = tickets[0]
    reg.retire(grant)
    delayed = j.recover_slot(t)
    response = None
    # Stop the repair after sealing, BEFORE its read. Another worker finishes.
    for _ in range(2):
        response = storage.call(delayed.send(response))
    j.run(j.recover_slot(t))
    acknowledge(j, reg, t)
    j.run(j.close_epoch(grant, retain_until=0))
    j.run(j.purge(t, now=1))
    request = delayed.send(response)
    assert isinstance(request, Read)
    with pytest.raises(Conflict):
        delayed.send(storage.call(request))
    assert t.column not in storage.rows[grant.row_key(0)]
    with pytest.raises(Conflict):
        j.run(j.recover_slot(t))
    j.run(j.close_epoch(grant, retain_until=0))  # Closed replay remains a no-op.


def test_retention_cannot_precede_bound_authorization_window() -> None:
    registry = InMemoryEpochRegistry()
    grant = Grant('workspace', 'us-central1', 'retention', 100, 1)
    registry.register(grant, Settings(async_settlement_journal_minimum_shard_cap_micros=1), tier=1)
    ticket = registry.allocate(grant, authorization='a', generation='g', key_id='k', nonce='n',
                               snapshot_hash='a' * 64, idempotency_until=1000)
    j = Journal(InMemoryJournalStorage(), registry, registry)
    j.run(j.initialize(grant, 0))
    j.run(j.create(ticket))
    registry.retire(grant)
    j.run(j.recover_slot(ticket))
    acknowledge(j, registry, ticket)
    with pytest.raises(Conflict, match='idempotency'):
        j.run(j.close_epoch(grant, retain_until=999))
    j.run(j.close_epoch(grant, retain_until=1000))
    with pytest.raises(Conflict):
        j.run(j.purge(ticket, now=999))
    j.run(j.purge(ticket, now=1000))


def test_missing_slot_repair_uses_fresh_version_across_other_worker_purge() -> None:
    j, storage, reg, grant, tickets = setup(count=1)
    t = tickets[0]
    storage.rows[grant.row_key(0)].pop(t.column)
    reg.retire(grant)
    delayed = j.recover_slot(t)
    response = None
    for _ in range(3):  # Includes a snapshot showing a missing slot.
        response = storage.call(delayed.send(response))
    j.run(j.recover_slot(t))
    acknowledge(j, reg, t)
    j.run(j.close_epoch(grant, retain_until=0))
    j.run(j.purge(t, now=1))
    cas = delayed.send(response)
    assert storage.call(cas) is False
    with pytest.raises(RetryLater):
        delayed.send(False)
    assert t.column not in storage.rows[grant.row_key(0)]


def test_pending_ticket_and_retention_timestamp_have_hard_size_bounds() -> None:
    _, _, _, _, tickets = setup(count=1)
    with pytest.raises(ValueError, match='deadline'):
        replace(tickets[0], idempotency_until=2**63)
    with pytest.raises(ValueError, match='identity'):
        replace(tickets[0], authorization='\0' * 256, generation='\0' * 256,
                key_id='\0' * 256, nonce='\0' * 256)


@pytest.mark.parametrize('damage', ['hash_missing', 'hash_changed', 'envelope_changed', 'ack_int',
                                   'charge_bool', 'usage_type', 'endpoint_type', 'extra',
                                   'missing_receipt', 'undercount', 'unknown_status'])
def test_corrupt_terminal_fails_closed(damage: str) -> None:
    j, storage, _, grant, tickets = setup(count=1)
    t = tickets[0]
    e = Envelope('settle', 'endpoint', 75)
    j.run(j.accept(t, e))
    row = storage.rows[grant.row_key(0)]
    state = json.loads(row[t.column])
    if damage == 'hash_missing':
        del state['hash']
    elif damage == 'hash_changed':
        state['hash'] = 'f' * 64
    elif damage == 'envelope_changed':
        state['envelope']['endpoint'] = 'different'
    elif damage == 'ack_int':
        state['ack'] = 1
    elif damage == 'charge_bool':
        state['envelope']['charge'] = True
    elif damage == 'usage_type':
        state['envelope']['usage'] = 'input'
    elif damage == 'endpoint_type':
        state['envelope']['endpoint'] = 3
    elif damage == 'extra':
        state['extra'] = 'unknown'
    elif damage == 'missing_receipt':
        state['ack'] = True
    elif damage == 'undercount':
        row[b'outstanding'] = b'0'
    else:
        state['status'] = 'unknown'
    row[t.column] = encode(state)
    for op in (j.accept(t, e), j.accept(t, Envelope('refund', 'endpoint', 0)), j.fence(t)):
        with pytest.raises(JournalError):
            j.run(op)


@pytest.mark.parametrize('kind', ['pending', 'refund', 'recovery', 'sync_required'])
def test_close_zero_debt_unresolved(kind: str) -> None:
    j, storage, reg, grant, tickets = setup(count=1)
    t = tickets[0]
    if kind == 'refund':
        j.run(j.accept(t, Envelope('refund', 'endpoint', 0)))
    elif kind == 'sync_required':
        j.run(j.accept(t, Envelope('settle', 'endpoint', 101)))
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    if kind == 'recovery':
        storage.rows[grant.row_key(0)].pop(t.column)
        j.run(j.recover_slot(t))
    assert storage.rows[grant.row_key(0)][b'outstanding'] == b'0'
    with pytest.raises(Conflict, match='unresolved'):
        j.run(j.close_epoch(grant, retain_until=0))
    assert reg.retention_deadline(grant) is None


@pytest.mark.parametrize('slot', [0, 63], ids=['first_page', 'final_page'])
def test_close_zero_debt_unregistered_slot(slot: int) -> None:
    j, storage, reg, grant, _ = setup(count=0)
    unregistered = Ticket(grant, 0, slot, 'unregistered-auth', 'unregistered-gen',
                          'key', 'nonce', 'a' * 64, 0)
    # Inject a pending slot without a registry binding on either closure page.
    storage.rows[grant.row_key(0)][unregistered.column] = unregistered.pending
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    assert not reg.registered(unregistered)
    assert storage.rows[grant.row_key(0)][b'outstanding'] == b'0'
    with pytest.raises(JournalError, match='unregistered or missing slot in closure page'):
        j.run(j.close_epoch(grant, retain_until=0))
    assert reg.retention_deadline(grant) is None


def test_close_inconsistent_debt_independent_of_slots() -> None:
    j, storage, reg, grant, _ = setup(count=0)
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    storage.rows[grant.row_key(0)][b'outstanding'] = b'1'
    with pytest.raises(Conflict, match='outstanding debt'):
        j.run(j.close_epoch(grant, retain_until=0))


def test_verified_receipt_before_terminal_selection_rejected() -> None:
    j, storage, reg, grant, tickets = setup(count=1)
    t = tickets[0]
    proof = Receipt(t.authorization, grant.epoch, '', 'committed')
    reg.record_ledger_receipt(t, proof)
    before = dict(storage.rows[grant.row_key(0)])
    with pytest.raises(Conflict, match='terminal choice'):
        j.run(j.acknowledge(t, proof))
    assert storage.rows[grant.row_key(0)] == before


def test_failed_fence_predicate_pending_read_fails_closed() -> None:
    j, storage, _, _, tickets = setup(count=1)
    original = storage.call
    storage.call = lambda req: False if isinstance(req, Compare) else original(req)
    with pytest.raises(JournalError, match='predicate semantics'):
        j.run(j.fence(tickets[0]))


def test_ack_nonaccepted_never_releases_other_slot_debt() -> None:
    j, storage, reg, grant, tickets = setup(count=2)
    j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 90)))
    assert j.run(j.accept(tickets[1], Envelope('settle', 'endpoint', 50))).status == 'sync_required'
    acknowledge(j, reg, tickets[1])
    assert storage.rows[grant.row_key(0)][b'outstanding'] == b'90'
    invariant(storage, grant)


@pytest.mark.parametrize('column', [b'shards', b'slots'])
@pytest.mark.parametrize('operation', ['accept', 'create', 'ack', 'recover', 'close', 'bound', 'purge'])
def test_grant_sizing_validated_on_reads(column: bytes, operation: str) -> None:
    j, storage, reg, grant, tickets = setup(count=1)
    t = tickets[0]
    if operation in ('ack', 'close', 'purge'):
        j.run(j.accept(t, Envelope('refund', 'endpoint', 0)))
    proof = receipt(j, t)
    reg.record_ledger_receipt(t, proof)
    if operation in ('close', 'purge'):
        j.run(j.acknowledge(t, proof))
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    if operation == 'purge':
        j.run(j.close_epoch(grant, retain_until=0))
    storage.rows[grant.row_key(0)][column] = b'123'
    ops = {'accept': j.accept(t, Envelope('refund', 'endpoint', 0)), 'create': j.create(t),
           'ack': j.acknowledge(t, proof), 'recover': j.recover_slot(t),
           'close': j.close_epoch(grant, retain_until=0), 'bound': j.bound_epoch(grant),
           'purge': j.purge(t, now=1)}
    with pytest.raises(JournalError, match='sizing'):
        j.run(ops[operation])


def test_sizing_policy_and_rotation_thresholds() -> None:
    from trusted_router.settlement_journal import Sizing, choose_sizing, rotation_due

    assert choose_sizing(5_000_000) == Sizing(128, 4096)
    assert choose_sizing(5_000_000, 70, 1) == Sizing(41, 4096)
    assert choose_sizing(5_000_000, 200, 1) == Sizing(117, 4096)
    assert choose_sizing(5_000_000, 10000, 1).shards == 156
    assert choose_sizing(25_000_000, 10000, 1).shards == 781
    assert choose_sizing(100_000_000, 10000, 1).shards == 3125
    assert choose_sizing(5_000_000, 0.1, 1).slots == 1024
    assert choose_sizing(5_000_000, 0.1, 10000).slots == 4096
    assert choose_sizing(5_000_000, minimum_shard_cap=100_000).shards == 50
    with pytest.raises(Conflict):
        choose_sizing(31_999)
    g = Grant('w', 'r', 'e', 100, 2, 64)
    assert not rotation_due(g, (31, 32))
    assert rotation_due(g, (32, 32))
    assert rotation_due(g, (58, 0))
    assert not rotation_due(g, (57, 0))
    with pytest.raises(Conflict, match='minimum'):
        InMemoryEpochRegistry().register(g, Settings(), tier=1)


@pytest.mark.parametrize('x', ['a' * 256, '\\' * 128])
def test_measured_maximal_slot_sizes(x: str) -> None:
    from dataclasses import asdict

    from trusted_router.settlement_journal import MAX_SLOT_BYTES

    # Either maximal UTF-8 bytes or maximal escaping; both fill the JSON budget.
    g = Grant(x, x, x, 100_000_000, 4096, 4096)
    t = Ticket(g, 4095, 4095, x, x, x, x, 'f' * 64, 2**63 - 1)
    e = Envelope('settle', x, 2**63 - 1, tuple((k, 2**63 - 1) for k in
                 sorted(('input', 'output', 'cached_read', 'cache_creation', 'reasoning'))))
    accepted = replace(e, charge=g.shard_cap(t.shard)).payload(t)
    largest_payload = e.payload(t)
    states = {'pending': json.loads(t.pending), 'accepted': accepted,
              'fenced': {**json.loads(t.pending), 'status': 'fenced'},
              'payload_fenced': {**largest_payload, 'status': 'fenced'},
              'sync_required': {**largest_payload, 'status': 'sync_required'},
              'acknowledged': {**largest_payload, 'status': 'sync_required', 'ack': True, 'receipt': x}}
    sizes = {name: len(encode(value)) for name, value in states.items()}
    assert len(encode(asdict(t))) == 2142
    assert sizes == {'pending': 2184, 'accepted': 2753, 'fenced': 2183,
                     'payload_fenced': 2765, 'sync_required': 2772, 'acknowledged': 3040}
    assert max(sizes.values()) <= MAX_SLOT_BYTES == 3072
    assert 4096 * MAX_SLOT_BYTES == 12 * 1024**2


def test_close_pages_cover_high_slots_and_bound_each_rpc() -> None:
    from trusted_router.settlement_journal import ReadPage

    reg = InMemoryEpochRegistry()
    grant = Grant('w', 'r', 'pages', 100, 1, 4096)
    reg.register(grant, Settings(async_settlement_journal_minimum_shard_cap_micros=1), tier=1)
    storage = InMemoryJournalStorage()
    j = Journal(storage, reg, reg)
    j.run(j.initialize(grant, 0))
    t = Ticket(grant, 0, 4095, 'a', 'g', 'k', 'n', 'a'*64, 0)
    reg.bind(t)
    j.run(j.create(t))
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    reads = []
    original = storage.call

    def record(req: Any) -> Any:
        if isinstance(req, ReadPage):
            reads.append(req)
        return original(req)

    storage.call = record
    with pytest.raises(Conflict, match='unresolved'):
        j.run(j.close_epoch(grant, retain_until=0))
    assert len(reads) == 69
    assert all(r.stop-r.start <= 60 for r in reads)
    j.run(j.fence(t))
    acknowledge(j, reg, t)
    j.run(j.close_epoch(grant, retain_until=0))
    assert reg.retention_deadline(grant) == 0


def rotation_settings() -> Settings:
    # Keep the tier cap real; use one shard so charges exercise cap reservation.
    return Settings(async_settlement_journal_minimum_shard_cap_micros=1)


def test_overlap_counts_every_older_epoch_and_replays_once() -> None:
    from trusted_router.settlement_journal import Sizing

    j, storage, reg, first, tickets = setup(cap=5_000_000, count=1)
    j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 2_000_000)))
    second = j.run(j.rotate(first, 'second', rotation_settings(), tier=1, sizing=Sizing(1, 64)))
    assert second.cap == 3_000_000
    j.run(j.initialize(second, 0))
    t2 = reg.allocate(second, authorization='second-auth', generation='g', key_id='k', nonce='n',
                      snapshot_hash='a'*64, idempotency_until=0)
    j.run(j.create(t2))
    j.run(j.accept(t2, Envelope('settle', 'endpoint', 1_000_000)))
    third = j.run(j.rotate(second, 'third', rotation_settings(), tier=1, sizing=Sizing(1, 64)))
    assert third.cap == 2_000_000  # Oldest 2M still counts, even though second only owes 1M.
    assert j.run(j.rotate(first, 'second', rotation_settings(), tier=1, sizing=Sizing(1, 64))) == second
    assert j.run(j.rotate(second, 'third', rotation_settings(), tier=1, sizing=Sizing(1, 64))) == third
    assert len(reg.grants()) == 3
    with pytest.raises(Conflict, match='immutable'):
        j.run(j.rotate(second, 'different', rotation_settings(), tier=1, sizing=Sizing(1, 64)))
    for g in (first, second):
        invariant(storage, g)


def test_grant_requires_every_shard_sealed() -> None:
    from trusted_router.settlement_journal import Sizing

    j, storage, reg, grant, _ = setup(cap=100, shards=2, count=0)
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    with pytest.raises(Conflict, match='sealed'):
        j.run(j.bound_epoch(grant))
    rows = tuple(storage.call(Read(grant.row_key(s), META)) for s in range(2))
    with pytest.raises(Conflict, match='sealed'):
        reg.record_bound(grant, rows)
    with pytest.raises(Conflict, match='bounds'):
        reg.successor(grant, 'next', rotation_settings(), tier=1, sizing=Sizing(1, 64))
    assert len(reg.grants()) == 1


@pytest.mark.parametrize('seed', range(30))
def test_overlap_random_crashes_preserve_workspace_cap(seed: int) -> None:
    from trusted_router.settlement_journal import Sizing

    rng = random.Random(seed)  # noqa: S311 -- deterministic schedule exploration
    j, storage, reg, grant, tickets = setup(cap=5_000_000, shards=2, count=6)
    all_tickets = list(tickets)
    history: dict[bytes, bytes] = {}
    for epoch in range(2):
        tasks = [Task(j.accept(t, Envelope('settle', 'endpoint', rng.randint(1, 1_000_000))))
                 for t in all_tickets]
        def rotate(previous: Grant = grant, name: str = f'next-{epoch}') -> Operation:
            return j.rotate(previous, name, rotation_settings(), tier=1, sizing=Sizing(2, 64))
        rotation = Task(rotate())
        for _ in range(200):
            active = [t for t in tasks if not t.done]
            selected = rng.choice([rotation, *active])
            if not selected.done:
                selected.step(storage)
                if rng.random() < 0.15:
                    if selected is rotation:
                        rotation = Task(rotate())  # Crash including lost grant result.
                    else:
                        selected.done = True
            for t in all_tickets:
                row = storage.rows[t.grant.row_key(t.shard)]
                state = json.loads(row[t.column])
                if state['status'] != 'pending' and rng.random() < 0.02:
                    proof = receipt(j, t)
                    reg.record_ledger_receipt(t, proof)
                    tasks.append(Task(j.acknowledge(t, proof)))
            total = 0
            for g in reg.grants():
                if all(g.row_key(s) in storage.rows for s in range(g.shards)):
                    invariant(storage, g, history)
                    total += sum(int(storage.rows[g.row_key(s)][b'outstanding']) for s in range(g.shards))
            assert total <= 5_000_000
        successor = j.run(rotate())
        assert successor == j.run(rotate())
        grant = successor
        for s in range(grant.shards):
            j.run(j.initialize(grant, s))
        for i in range(6):
            t = reg.allocate(grant, authorization=f'{epoch}-{i}', generation='g', key_id='k',
                             nonce='n', snapshot_hash='a'*64, idempotency_until=0)
            j.run(j.create(t))
            all_tickets.append(t)
    # Fill the final epoch too, while older obligations can remain unresolved.
    for t in all_tickets:
        if t.grant == grant:
            j.run(j.accept(t, Envelope('settle', 'endpoint', 1_000_000)))
    assert sum(int(row[b'outstanding']) for row in storage.rows.values()) <= 5_000_000


@pytest.mark.parametrize('crash_after', range(10))
def test_rotation_crash_boundaries(crash_after: int) -> None:
    from trusted_router.settlement_journal import Sizing

    j, storage, reg, grant, tickets = setup(cap=5_000_000, shards=2, count=2)
    j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 2_000_000)))
    # Individual RPC crashes (including after the last sealed-bound read).
    task = Task(j.rotate(grant, 'next', rotation_settings(), tier=1, sizing=Sizing(2, 64)))
    for _ in range(crash_after):
        if not task.done:
            task.step(storage)
    # New process facade, same persisted registry/row state; response discarded.
    recovered = Journal(storage, reg, reg)
    successor = recovered.run(recovered.rotate(grant, 'next', rotation_settings(), tier=1, sizing=Sizing(2, 64)))
    assert successor.cap == 3_000_000
    assert len(reg.grants()) == 2
    assert all(storage.rows[grant.row_key(s)][b'sealed'] == b'1' for s in range(2))


@pytest.mark.parametrize('stage', ['retire', 'seal', 'bound', 'grant'])
def test_registry_rotation_checkpoint_crashes(stage: str) -> None:
    from trusted_router.settlement_journal import Sizing

    j, storage, reg, grant, tickets = setup(cap=5_000_000, count=1)
    j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 2_000_000)))
    reg.retire(grant)
    if stage in ('seal', 'bound', 'grant'):
        j.run(j.seal_shard(grant, 0))
    if stage in ('bound', 'grant'):
        j.run(j.bound_epoch(grant))
    if stage == 'grant':
        reg.successor(grant, 'next', rotation_settings(), tier=1, sizing=Sizing(1, 64))
    # Crash after durable registry checkpoints, with their responses lost.
    assert len(reg.grants()) == (2 if stage == 'grant' else 1)
    j = Journal(storage, reg, reg)
    next_grant = j.run(j.rotate(grant, 'next', rotation_settings(), tier=1, sizing=Sizing(1, 64)))
    assert next_grant.cap == 3_000_000
    assert len(reg.grants()) == 2


def test_rotation_waits_for_capacity_then_refreshes_conservative_bound() -> None:
    from trusted_router.settlement_journal import Sizing

    j, _, reg, grant, tickets = setup(cap=5_000_000, count=1)
    j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', grant.cap)))
    with pytest.raises(RetryLater, match='capacity'):
        j.run(j.rotate(grant, 'next', rotation_settings(), tier=1, sizing=Sizing(1, 64)))
    assert len(reg.grants()) == 1
    acknowledge(j, reg, tickets[0])
    successor = j.run(j.rotate(grant, 'next', rotation_settings(), tier=1, sizing=Sizing(1, 64)))
    assert successor.cap == 5_000_000
    assert len(reg.grants()) == 2


def test_concurrent_successor_reservation_has_one_winner() -> None:
    from concurrent.futures import ThreadPoolExecutor

    from trusted_router.settlement_journal import Sizing

    j, _, reg, grant, tickets = setup(cap=5_000_000, count=1)
    j.run(j.accept(tickets[0], Envelope('settle', 'endpoint', 2_000_000)))
    reg.retire(grant)
    j.run(j.seal_shard(grant, 0))
    j.run(j.bound_epoch(grant))

    def reserve(epoch: str) -> Grant | None:
        try:
            return reg.successor(grant, epoch, rotation_settings(), tier=1, sizing=Sizing(1, 64))
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        winners = [g for g in pool.map(reserve, ['next-a', 'next-b'] * 4) if g is not None]
    assert len(winners) == 4
    assert len(set(winners)) == 1
    assert winners[0].cap == 3_000_000
    assert len(reg.grants()) == 2


def test_grant_slot_count_controls_allocation_and_binding_is_immutable() -> None:
    reg = InMemoryEpochRegistry()
    grant = Grant('w', 'r', 'e', 100, 1, 128)
    reg.register(grant, rotation_settings(), tier=1)
    for i in range(128):
        t = reg.allocate(grant, authorization=f'a{i}', generation='g', key_id='k', nonce='n',
                         snapshot_hash='a'*64, idempotency_until=0)
        assert t.slot == i
    with pytest.raises(Conflict, match='exhausted'):
        reg.allocate(grant, authorization='overflow', generation='g', key_id='k', nonce='n',
                     snapshot_hash='a'*64, idempotency_until=0)
    with pytest.raises(Conflict):
        reg.register(replace(grant, slots=256), rotation_settings(), tier=1)
    with pytest.raises(Conflict):
        reg.register(replace(grant, shards=2), rotation_settings(), tier=1)


def test_fence_cannot_publish_malformed_or_oversized_candidate() -> None:
    j, storage, _, grant, tickets = setup(count=1)
    t = tickets[0]
    before = dict(storage.rows[grant.row_key(0)])
    with pytest.raises(ValueError, match='requires a payload'):
        j.run(j.fence(t, status='sync_required'))
    desired = Envelope('settle', 'endpoint', 1).payload(t)
    desired['hash'] = 'bad'
    with pytest.raises(JournalError, match='hash'):
        j.run(j.fence(t, status='sync_required', desired=desired))
    desired['extra'] = 'x'*4096
    with pytest.raises(JournalError, match='large'):
        j.run(j.fence(t, desired=desired))
    assert storage.rows[grant.row_key(0)] == before
