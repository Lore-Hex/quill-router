"""Portable, RPC-steppable settlement journal. No route or pricing integration.

Each shard escrows a fixed part of one workspace grant. An intent and its
outstanding charge ALWAYS share a row and a conditional mutation. See
docs/async-settlement-journal.md for the protocol and its limitations.
"""
from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from collections.abc import Generator
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from trusted_router.config import Settings
from trusted_router.trust_eligibility import tier_cap

SHARDS = 128
SLOTS = 4096
MAX_SHARDS = 4096
MAX_SLOTS = 4096
PAGE_SLOTS = 60
# Canonical JSON expansion is bounded too (including ASCII control escapes).
MAX_IDENTIFIER_BYTES = 256
MAX_IDENTIFIER_JSON_BYTES = 258
MAX_ENVELOPE_BYTES = 3000
MAX_TICKET_BYTES = 2500
MAX_SLOT_BYTES = 3072
MAX_ATTEMPTS = 2
META = (b'version', b'cap', b'outstanding', b'sealed', b'shards', b'slots')


class JournalError(RuntimeError):
    """Unavailable/ambiguous is NOT permission to settle or release money."""


class Conflict(JournalError):
    pass


class RetryLater(JournalError):
    """Background lifecycle/ack operation needs another bounded attempt."""


def encode(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def token() -> bytes:
    return uuid.uuid4().hex.encode()


def identifier(value: str) -> None:
    if (not isinstance(value, str) or not value or
            len(value.encode()) > MAX_IDENTIFIER_BYTES or
            len(encode(value)) > MAX_IDENTIFIER_JSON_BYTES):
        raise ValueError('identity exceeds byte or canonical JSON bound')


@dataclass(frozen=True)
class Grant:
    workspace: str
    region: str
    epoch: str
    cap: int
    shards: int = SHARDS
    slots: int = SLOTS

    def __post_init__(self) -> None:
        for value in (self.workspace, self.region, self.epoch):
            identifier(value)
        if type(self.cap) is not int or not 0 < self.cap <= 100_000_000:
            raise ValueError('invalid grant cap')
        if type(self.shards) is not int or not 1 <= self.shards <= MAX_SHARDS:
            raise ValueError('invalid shard count')
        if type(self.slots) is not int or not 64 <= self.slots <= MAX_SLOTS:
            raise ValueError('invalid slot count')

    def row_key(self, shard: int) -> bytes:
        if type(shard) is not int or not 0 <= shard < self.shards:
            raise ValueError('invalid shard')
        identity = hashlib.sha256(encode([self.workspace, self.region, self.epoch])).hexdigest()
        spread = hashlib.sha256(encode([identity, shard])).hexdigest()[:8]
        return f'{spread}#settlement#{self.region.encode().hex()}#{identity}#{shard:04x}'.encode()

    def shard_cap(self, shard: int) -> int:
        return self.cap // self.shards + (shard < self.cap % self.shards)


@dataclass(frozen=True)
class Sizing:
    shards: int
    slots: int

    def __post_init__(self) -> None:
        if type(self.shards) is not int or not 1 <= self.shards <= MAX_SHARDS:
            raise ValueError('invalid shard count')
        if type(self.slots) is not int or not 64 <= self.slots <= MAX_SLOTS:
            raise ValueError('invalid slot count')


def choose_sizing(cap: int, peak_rate: float | None = None,
                  drain_lag: float | None = None, *, minimum_shard_cap: int = 32_000) -> Sizing:
    """Target <=5% three-mutation interference in a modeled 10ms CAS window.

    Slots target one hour or four drain lags before the 50% rotation trigger.
    Both are planning heuristics, not a throughput or fallback guarantee.
    """
    if type(cap) is not int or cap <= 0 or type(minimum_shard_cap) is not int or minimum_shard_cap <= 0:
        raise ValueError('invalid sizing cap')
    for value in (peak_rate, drain_lag):
        if value is not None and (not math.isfinite(value) or value < 0):
            raise ValueError('invalid traffic measurement')
    maximum = min(MAX_SHARDS, cap // minimum_shard_cap)
    if maximum < 1:
        raise Conflict('remaining grant cannot support minimum shard capacity')
    if peak_rate is None or drain_lag is None:
        return Sizing(min(SHARDS, maximum), SLOTS)
    shards = min(maximum, max(1, math.ceil(3 * peak_rate * 0.01 / -math.log(0.95))))
    needed = max(64, math.ceil(2 * peak_rate * max(3600, 4 * drain_lag) / shards))
    slots = min(MAX_SLOTS, 1 << (needed - 1).bit_length())
    return Sizing(shards, slots)


def rotation_due(grant: Grant, allocated_per_shard: tuple[int, ...]) -> bool:
    if (len(allocated_per_shard) != grant.shards or
            any(type(n) is not int or not 0 <= n <= grant.slots for n in allocated_per_shard)):
        raise ValueError('invalid allocation counts')
    return (2 * sum(allocated_per_shard) >= grant.shards * grant.slots or
            any(10 * n >= 9 * grant.slots for n in allocated_per_shard))


@dataclass(frozen=True)
class Ticket:
    """Verified authorize binding; never construct from an unverified HTTP body.

    Registry allocates an ordinal exactly once; consumers need no registry RPC.
    PR 6 must authenticate this entire binding and verify frozen-price equality.
    """
    grant: Grant
    shard: int
    slot: int
    authorization: str
    generation: str
    key_id: str
    nonce: str
    snapshot_hash: str
    idempotency_until: int
    snapshot_version: int = 1
    authority: str = 'credits_endpoint'

    def __post_init__(self) -> None:
        self.grant.row_key(self.shard)
        if type(self.slot) is not int or not 0 <= self.slot < self.grant.slots:
            raise ValueError('invalid slot')
        for value in (self.authorization, self.generation, self.key_id, self.nonce):
            identifier(value)
        if (not isinstance(self.snapshot_hash, str) or len(self.snapshot_hash) != 64 or
                any(c not in '0123456789abcdef' for c in self.snapshot_hash)):
            raise ValueError('invalid snapshot hash')
        if type(self.idempotency_until) is not int or not 0 <= self.idempotency_until < 2**63:
            raise ValueError('invalid idempotency deadline')
        if type(self.snapshot_version) is not int or self.snapshot_version != 1 or self.authority != 'credits_endpoint':
            raise ValueError('unsupported billing contract')
        if len(encode(asdict(self))) > MAX_TICKET_BYTES:
            raise ValueError('ticket too large')

    @property
    def column(self) -> bytes:
        return f's/{self.slot:04x}'.encode()

    @property
    def pending(self) -> bytes:
        return encode({'ticket': asdict(self), 'status': 'pending', 'ack': False})


@dataclass(frozen=True)
class Envelope:
    """Exact verified amount, never re-priced here. Only bounded accounting data."""
    kind: Literal['settle', 'refund']
    endpoint: str
    charge: int
    # Immutable normalized usage, no arbitrary JSON, prompts or credentials.
    usage: tuple[tuple[str, int], ...] = ()

    def __post_init__(self) -> None:
        identifier(self.endpoint)
        if self.kind not in ('settle', 'refund'):
            raise ValueError('invalid terminal kind')
        if type(self.charge) is not int or not 0 <= self.charge < 2**63:
            raise ValueError('invalid charge')
        if self.kind == 'refund' and self.charge:
            raise ValueError('refund must have zero charge')
        names = [k for k, _ in self.usage]
        allowed = {'input', 'output', 'cached_read', 'cache_creation', 'reasoning'}
        if len(set(names)) != len(names) or not set(names) <= allowed:
            raise ValueError('invalid normalized usage')
        if any(type(v) is not int or not 0 <= v < 2**63 for _, v in self.usage):
            raise ValueError('invalid normalized count')
        if names != sorted(names):
            raise ValueError('usage must be canonical')

    def payload(self, ticket: Ticket) -> dict[str, Any]:
        body = {'ticket': asdict(ticket), 'envelope': asdict(self)}
        raw = encode(body)
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise ValueError('envelope too large')
        return {**body, 'hash': hashlib.sha256(raw).hexdigest(),
                'status': 'accepted', 'ack': False}


@dataclass(frozen=True)
class Receipt:
    authorization: str
    epoch: str
    payload_hash: str  # Empty only for a recovery fence without a submitted payload.
    receipt_id: str

    def __post_init__(self) -> None:
        for value in (self.authorization, self.epoch, self.receipt_id):
            identifier(value)


class EpochRegistry(Protocol):
    """PR 4 port. Must durably enumerate grants/slots before issuing tickets.

    One active grant per workspace, globally unique authorization binding, no
    recycling ordinals or epochs. Retirement stops allocation before sealing.
    Successor reservation subtracts bounds of ALL unclosed epochs atomically.
    close() requires every shard sealed and every slot acknowledged.
    Persist grant K/S, bounds, successors and tombstones; never region-migrate.
    """
    def grants(self) -> tuple[Grant, ...]: ...
    def tickets(self, grant: Grant) -> tuple[Ticket, ...]: ...
    def registered(self, ticket: Ticket) -> bool: ...
    def retire(self, grant: Grant) -> None: ...
    def is_retired(self, grant: Grant) -> bool: ...
    def close(self, grant: Grant, *, retain_until: int) -> None: ...
    def retention_deadline(self, grant: Grant) -> int | None: ...
    def record_bound(self, grant: Grant, rows: tuple[dict[bytes, bytes], ...]) -> None: ...
    def successor(self, previous: Grant, epoch: str, settings: Settings, *, tier: int,
                  sizing: Sizing) -> Grant: ...


class ReceiptVerifier(Protocol):
    """Verify a durable ledger-finalization receipt, NEVER an import receipt."""
    def verify(self, ticket: Ticket, receipt: Receipt) -> bool: ...


class InMemoryEpochRegistry:
    """Test implementation only. Registry calls aren't on the handoff path."""
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._grants: dict[Grant, str] = {}
        self._tickets: dict[str, Ticket] = {}
        self._slots: dict[tuple[Grant, int, int], str] = {}
        self._epoch_tickets: dict[Grant, list[Ticket]] = {}
        self._deadlines: dict[Grant, int] = {}
        self._receipts: set[tuple[Ticket, Receipt]] = set()
        self._bounds: dict[Grant, int] = {}
        self._successors: dict[Grant, Grant] = {}

    def register(self, grant: Grant, settings: Settings, *, tier: int | None) -> None:
        # No permissive spend_cap()/legacy max fallback, including tier=None.
        if type(tier) is not int or tier not in (1, 2, 3) or grant.cap > tier_cap(settings, tier):
            raise Conflict('workspace is ineligible or grant exceeds trust cap')
        if grant.cap // grant.shards < settings.async_settlement_journal_minimum_shard_cap_micros:
            raise Conflict('grant below minimum shard capacity')
        with self._lock:
            if grant in self._grants:
                return
            if any(g.workspace == grant.workspace and s != 'closed'
                   for g, s in self._grants.items()):
                raise Conflict('workspace already has an outstanding epoch')
            if any(g.workspace == grant.workspace and g.epoch == grant.epoch
                   for g in self._grants):
                raise Conflict('epoch cannot be recycled')
            self._grants[grant] = 'active'

    def allocate(self, grant: Grant, *, authorization: str, generation: str,
                 key_id: str, nonce: str, snapshot_hash: str, idempotency_until: int) -> Ticket:
        """Stable hashed shard, no probing/borrowing budget across shards."""
        shard = int.from_bytes(hashlib.sha256(authorization.encode()).digest()[:8]) % grant.shards
        with self._lock:
            old = self._tickets.get(authorization)
            if old is not None:
                candidate = Ticket(grant, shard, old.slot, authorization, generation,
                                   key_id, nonce, snapshot_hash, idempotency_until)
                self.bind(candidate)
                return candidate
            slot = next((i for i in range(grant.slots) if (grant, shard, i) not in self._slots), None)
            if slot is None:
                raise Conflict('shard slots exhausted; synchronous authorization required')
            ticket = Ticket(grant, shard, slot, authorization, generation, key_id, nonce,
                            snapshot_hash, idempotency_until)
            self.bind(ticket)
            return ticket

    def bind(self, ticket: Ticket) -> None:
        with self._lock:
            old = self._tickets.get(ticket.authorization)
            if old is not None:
                if old != ticket:
                    raise Conflict('authorization binding is immutable')
                return
            if self._grants.get(ticket.grant) != 'active':
                raise Conflict('epoch is not active')
            if (ticket.grant, ticket.shard, ticket.slot) in self._slots:
                raise Conflict('slot is already allocated')
            self._tickets[ticket.authorization] = ticket
            self._slots[(ticket.grant, ticket.shard, ticket.slot)] = ticket.authorization
            self._epoch_tickets.setdefault(ticket.grant, []).append(ticket)

    def grants(self) -> tuple[Grant, ...]:
        with self._lock:
            return tuple(self._grants)

    def tickets(self, grant: Grant) -> tuple[Ticket, ...]:
        with self._lock:
            return tuple(self._epoch_tickets.get(grant, []))

    def registered(self, ticket: Ticket) -> bool:
        with self._lock:
            return self._tickets.get(ticket.authorization) == ticket

    def retire(self, grant: Grant) -> None:
        with self._lock:
            if self._grants[grant] != 'closed':
                self._grants[grant] = 'retiring'

    def is_retired(self, grant: Grant) -> bool:
        with self._lock:
            return self._grants.get(grant) in ('retiring', 'closed')

    def close(self, grant: Grant, *, retain_until: int) -> None:
        with self._lock:
            if not self.is_retired(grant):
                raise Conflict('retire before close')
            self._grants[grant] = 'closed'
            self._deadlines[grant] = max(retain_until, self._deadlines.get(grant, 0))

    def retention_deadline(self, grant: Grant) -> int | None:
        with self._lock:
            return self._deadlines.get(grant)

    def record_bound(self, grant: Grant, rows: tuple[dict[bytes, bytes], ...]) -> None:
        """Trusted journal-only evidence; production port must persist atomically.

        Rows are ordered by shard and obtained by exact-key reads after sealing.
        Never expose this method to untrusted client-supplied evidence.
        """
        with self._lock:
            if not self.is_retired(grant) or len(rows) != grant.shards:
                raise Conflict('bound requires every retired shard')
            bound = 0
            for shard, row in enumerate(rows):
                validate_metadata(row, grant, shard)
                if row[b'sealed'] not in (b'1', b'2'):
                    raise Conflict('bound requires every shard sealed')
                bound += int(row[b'outstanding'])
            self._bounds[grant] = min(bound, self._bounds.get(grant, grant.cap))

    def successor(self, previous: Grant, epoch: str, settings: Settings, *, tier: int,
                  sizing: Sizing) -> Grant:
        """One registry transaction: reserve remainder AND bind successor once.

        The in-memory reference models atomic persistence; a production registry
        must commit these records together and retain them across process loss.
        """
        with self._lock:
            if type(tier) is not int or tier not in (1, 2, 3):
                raise Conflict('workspace is ineligible')
            old = self._successors.get(previous)
            if old is not None:
                if (old.epoch, old.shards, old.slots) != (epoch, sizing.shards, sizing.slots):
                    raise Conflict('successor transition is immutable')
                return old
            if previous not in self._grants or not self.is_retired(previous):
                raise Conflict('retire before successor')
            open_grants = [g for g, state in self._grants.items()
                           if g.workspace == previous.workspace and state != 'closed']
            if any(not self.is_retired(g) or g not in self._bounds for g in open_grants):
                raise Conflict('all older epochs require sealed bounds')
            remaining = tier_cap(settings, tier) - sum(self._bounds[g] for g in open_grants)
            if remaining <= 0:
                raise RetryLater('all capacity remains outstanding')
            if remaining // sizing.shards < settings.async_settlement_journal_minimum_shard_cap_micros:
                raise RetryLater('choose fewer shards for remaining capacity')
            grant = Grant(previous.workspace, previous.region, epoch, remaining,
                          sizing.shards, sizing.slots)
            if any(g.workspace == grant.workspace and g.epoch == epoch for g in self._grants):
                raise Conflict('epoch cannot be recycled')
            self._grants[grant] = 'active'
            self._successors[previous] = grant
            return grant

    def record_ledger_receipt(self, ticket: Ticket, receipt: Receipt) -> None:
        with self._lock:
            self._receipts.add((ticket, receipt))

    def verify(self, ticket: Ticket, receipt: Receipt) -> bool:
        with self._lock:
            return ((ticket, receipt) in self._receipts and
                    receipt.authorization == ticket.authorization and
                    receipt.epoch == ticket.grant.epoch and bool(receipt.receipt_id))


@dataclass(frozen=True)
class Read:
    key: bytes
    columns: tuple[bytes, ...]


@dataclass(frozen=True)
class ReadPage:
    key: bytes
    start: int
    stop: int

    def __post_init__(self) -> None:
        if not 0 <= self.start < self.stop <= MAX_SLOTS or self.stop - self.start > PAGE_SLOTS:
            raise ValueError('bounded slot page required')


@dataclass(frozen=True)
class Compare:
    key: bytes
    column: bytes
    expected: bytes | None
    updates: dict[bytes, bytes | None]


Request = Read | ReadPage | Compare
Operation = Generator[Request, Any, Any]


class RowStorage(Protocol):
    """Only a point-read or a per-row atomic conditional mutation; no tx lock."""
    def call(self, request: Request) -> Any: ...


@dataclass
class Metrics:
    cas_conflicts: int = 0
    contention_fallbacks: int = 0
    rpc_calls: int = 0
    _lock: Any = field(default_factory=threading.Lock, repr=False, compare=False)

    def count(self, counter: str) -> None:
        with self._lock:
            setattr(self, counter, getattr(self, counter) + 1)


@dataclass(frozen=True)
class Outcome:
    status: str
    payload_hash: str = ''
    charge: int = 0


def result(state: dict[str, Any]) -> Outcome:
    return Outcome(state['status'], state.get('hash', ''),
                   state.get('envelope', {}).get('charge', 0))


def validate_metadata(row: dict[bytes, bytes], grant: Grant, shard: int) -> None:
    try:
        if not all(c in row for c in META):
            raise ValueError('incomplete shard')
        outstanding, cap = int(row[b'outstanding']), int(row[b'cap'])
        if not 0 <= outstanding <= cap == grant.shard_cap(shard):
            raise ValueError('corrupt shard accounting')
        if (row[b'shards'] != str(grant.shards).encode() or
                row[b'slots'] != str(grant.slots).encode()):
            raise ValueError('grant sizing mismatch')
        if row[b'sealed'] not in (b'0', b'1', b'2') or not row[b'version']:
            raise ValueError('invalid shard state')
    except (ValueError, KeyError, TypeError) as exc:
        raise JournalError(str(exc)) from exc


def slot_state(row: dict[bytes, bytes], ticket: Ticket) -> dict[str, Any]:
    validate_metadata(row, ticket.grant, ticket.shard)
    try:
        raw = row[ticket.column]
        if len(raw) > MAX_SLOT_BYTES:
            raise ValueError('slot too large')
        state: dict[str, Any] = json.loads(raw)
        if not isinstance(state, dict) or encode(state['ticket']) != encode(asdict(ticket)):
            raise Conflict('ticket binding mismatch')
        status = state['status']
        if status not in ('pending', 'accepted', 'fenced', 'sync_required') or type(state['ack']) is not bool:
            raise ValueError('invalid slot status or acknowledgment')
        fields = {'ticket', 'status', 'ack'}
        if status == 'pending' and state['ack']:
            raise ValueError('pending slot acknowledged')
        if status in ('accepted', 'sync_required') or 'envelope' in state:
            body = state['envelope']
            if not isinstance(body, dict) or set(body) != {'kind', 'endpoint', 'charge', 'usage'}:
                raise ValueError('invalid envelope fields')
            if (not isinstance(body['usage'], list) or
                    any(not isinstance(v, list) or len(v) != 2 or not isinstance(v[0], str)
                        for v in body['usage'])):
                raise ValueError('invalid usage types')
            envelope = Envelope(body['kind'], body['endpoint'], body['charge'],
                                tuple((v[0], v[1]) for v in body['usage']))
            expected = envelope.payload(ticket)['hash']
            if state['hash'] != expected:
                raise ValueError('canonical payload hash mismatch')
            fields |= {'envelope', 'hash'}
            if status == 'pending':
                raise ValueError('pending slot contains payload')
            if status == 'accepted' and not state['ack'] and envelope.charge > int(row[b'outstanding']):
                raise ValueError('counter undercounts charge')
        if state['ack']:
            identifier(state['receipt'])
            fields.add('receipt')
        if set(state) != fields:
            raise ValueError('invalid status-specific fields')
        return state
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise JournalError('malformed slot: ' + str(exc)) from exc


@dataclass
class Journal:
    storage: RowStorage
    registry: EpochRegistry
    receipts: ReceiptVerifier
    metrics: Metrics = field(default_factory=Metrics)

    def run(self, operation: Operation) -> Any:
        response: Any = None
        while True:
            try:
                request = operation.send(response)
            except StopIteration as done:
                return done.value
            self.metrics.count('rpc_calls')
            # No automatic transport retry. An ambiguous write raises; retry
            # the operation by identity. Never translate an exception to sync.
            response = self.storage.call(request)

    def initialize(self, grant: Grant, shard: int) -> Operation:
        if grant not in self.registry.grants():
            raise Conflict('unregistered epoch')
        key = grant.row_key(shard)
        yield Compare(key, b'version', None, {
            b'version': token(), b'cap': str(grant.shard_cap(shard)).encode(),
            b'outstanding': b'0', b'sealed': b'0',
            b'shards': str(grant.shards).encode(), b'slots': str(grant.slots).encode(),
        })
        # Never reset existing state, including a sealed marker.

    def create(self, ticket: Ticket) -> Operation:
        if not self.registry.registered(ticket):
            raise Conflict('unregistered slot')
        key = ticket.grant.row_key(ticket.shard)
        for _ in range(MAX_ATTEMPTS):
            row = yield Read(key, (*META, ticket.column))
            validate_metadata(row, ticket.grant, ticket.shard)
            if ticket.column in row:
                return result(slot_state(row, ticket))
            if row.get(b'sealed') != b'0' or self.registry.is_retired(ticket.grant):
                raise Conflict('epoch retired or uninitialized')
            if (yield Compare(key, b'version', row[b'version'],
                              {ticket.column: ticket.pending, b'version': token()})):
                return Outcome('pending')
            self.metrics.count('cas_conflicts')
        raise RetryLater('slot creation contended; do not issue async ticket')

    def accept(self, ticket: Ticket, envelope: Envelope) -> Operation:
        desired = envelope.payload(ticket)
        key = ticket.grant.row_key(ticket.shard)
        for _ in range(MAX_ATTEMPTS):
            row = yield Read(key, (*META, ticket.column))
            state = slot_state(row, ticket)
            if state.get('hash') and state['hash'] != desired['hash']:
                raise Conflict('immutable terminal payload mismatch')
            if state['status'] != 'pending':
                return result(state)
            outstanding = int(row[b'outstanding'])
            if row[b'sealed'] != b'0' or outstanding + envelope.charge > int(row[b'cap']):
                return (yield from self.fence(ticket, status='sync_required', desired=desired))
            # The only budget increment: same atomic row mutation as intent.
            if (yield Compare(key, b'version', row[b'version'], {
                ticket.column: encode(desired),
                b'outstanding': str(outstanding + envelope.charge).encode(),
                b'version': token(),
            })):
                return result(desired)
            self.metrics.count('cas_conflicts')
        self.metrics.count('contention_fallbacks')
        return (yield from self.fence(ticket, status='sync_required', desired=desired))

    def fence(self, ticket: Ticket, *, status: str = 'fenced',
              desired: dict[str, Any] | None = None) -> Operation:
        if status not in ('fenced', 'sync_required'):
            raise ValueError('invalid fence')
        if status == 'sync_required' and desired is None:
            raise ValueError('synchronous fence requires a payload')
        key = ticket.grant.row_key(ticket.shard)
        state = dict(desired) if desired is not None else json.loads(ticket.pending)
        state['status'] = status
        if state.get('ack') is not False:
            raise ValueError('fence cannot acknowledge a slot')
        # Validate the candidate before publishing it; selected non-accepted
        # states carry no debt. This is local schema validation, not a row read.
        raw = encode(state)
        slot_state({ticket.column: raw, b'version': b'candidate', b'sealed': b'0',
                    b'cap': str(ticket.grant.shard_cap(ticket.shard)).encode(),
                    b'outstanding': b'0', b'shards': str(ticket.grant.shards).encode(),
                    b'slots': str(ticket.grant.slots).encode()}, ticket)
        # Independent of shared version: guaranteed terminal arbitration in
        # one mutation, even under continuous unrelated shard traffic.
        if (yield Compare(key, ticket.column, ticket.pending,
                          {ticket.column: raw, b'version': token()})):
            return result(state)
        row = yield Read(key, (*META, ticket.column))
        winner = slot_state(row, ticket)
        if winner['status'] == 'pending':
            raise JournalError('predicate semantics violated')
        if desired is not None and winner.get('hash'):
            if winner['hash'] != desired['hash']:
                raise Conflict('immutable terminal payload mismatch')
        return result(winner)

    def acknowledge(self, ticket: Ticket, receipt: Receipt) -> Operation:
        if not self.receipts.verify(ticket, receipt):
            raise Conflict('ledger finalization receipt not verified')
        key = ticket.grant.row_key(ticket.shard)
        for _ in range(MAX_ATTEMPTS):
            row = yield Read(key, (*META, ticket.column))
            state = slot_state(row, ticket)
            if state['status'] == 'pending' or state.get('hash', '') != receipt.payload_hash:
                raise Conflict('receipt does not match terminal choice')
            if state['ack']:
                return result(state)
            amount = state.get('envelope', {}).get('charge', 0) if state['status'] == 'accepted' else 0
            if amount > int(row[b'outstanding']):
                raise JournalError('counter undercounts charge')
            state['ack'] = True
            state['receipt'] = receipt.receipt_id
            if (yield Compare(key, b'version', row[b'version'], {
                ticket.column: encode(state),
                b'outstanding': str(int(row[b'outstanding']) - amount).encode(),
                b'version': token(),
            })):
                return result(state)
            self.metrics.count('cas_conflicts')
        raise RetryLater('ack contention; retain charge and replay receipt')

    def seal_shard(self, grant: Grant, shard: int) -> Operation:
        if not self.registry.is_retired(grant):
            raise Conflict('retire registry before sealing')
        # Initialize absent shards first, so delayed initialize cannot reopen.
        yield from self.initialize(grant, shard)
        yield Compare(grant.row_key(shard), b'sealed', b'0',
                      {b'sealed': b'1', b'version': token()})

    def recover_slot(self, ticket: Ticket) -> Operation:
        """Repair a crash between registry binding and slot creation.

        Absence alone never authorizes recovery. Seal first, read the missing
        slot, then fence under that version. Purge advances both the version
        and sealed=2; a delayed repair cannot resurrect an expired identity.
        """
        if (not self.registry.registered(ticket) or
                not self.registry.is_retired(ticket.grant) or
                self.registry.retention_deadline(ticket.grant) is not None):
            raise Conflict('missing-slot recovery requires an unclosed retired binding')
        yield from self.seal_shard(ticket.grant, ticket.shard)
        key = ticket.grant.row_key(ticket.shard)
        row = yield Read(key, (*META, ticket.column))
        validate_metadata(row, ticket.grant, ticket.shard)
        if ticket.column in row:
            return (yield from self.fence(ticket))
        if row.get(b'sealed') != b'1':
            raise Conflict('purged or unsealed shard cannot acquire recovery slots')
        state = json.loads(ticket.pending)
        state['status'] = 'fenced'
        if (yield Compare(key, b'version', row[b'version'],
                          {ticket.column: encode(state), b'version': token()})):
            return result(state)
        self.metrics.count('cas_conflicts')
        raise RetryLater('recovery contended; retain obligation and retry')

    def close_epoch(self, grant: Grant, *, retain_until: int) -> Operation:
        if self.registry.retention_deadline(grant) is not None:
            return
        if not self.registry.is_retired(grant):
            raise Conflict('retire registry first')
        by_shard: dict[int, list[Ticket]] = {}
        for ticket in self.registry.tickets(grant):
            if retain_until < ticket.idempotency_until:
                raise Conflict('retention deadline precedes authorization idempotency window')
            by_shard.setdefault(ticket.shard, []).append(ticket)
        for shard in range(grant.shards):
            tickets = by_shard.get(shard, [])
            row = yield Read(grant.row_key(shard), META)
            validate_metadata(row, grant, shard)
            if row[b'sealed'] != b'1' or row[b'outstanding'] != b'0':
                raise Conflict('epoch has unsealed shards or outstanding debt')
            for start in range(0, grant.slots, PAGE_SLOTS):
                stop = min(start + PAGE_SLOTS, grant.slots)
                page = yield ReadPage(grant.row_key(shard), start, stop)
                validate_metadata(page, grant, shard)
                expected = {t.column: t for t in tickets if start <= t.slot < stop}
                if {c for c in page if c.startswith(b's/')} != set(expected):
                    raise JournalError('unregistered or missing slot in closure page')
                for ticket in expected.values():
                    if not slot_state(page, ticket)['ack']:
                        raise Conflict('unresolved slot blocks epoch close')
        # Sealed shards cannot acquire new debt/slots, so sequential reads are
        # sufficient. Closing releases the old bound for subsequent rotations.
        self.registry.close(grant, retain_until=retain_until)

    def bound_epoch(self, grant: Grant) -> Operation:
        """Sealing is the acceptance barrier, not retirement alone."""
        if not self.registry.is_retired(grant):
            raise Conflict('retire before bound')
        rows = []
        for shard in range(grant.shards):
            row = yield Read(grant.row_key(shard), META)
            validate_metadata(row, grant, shard)
            if row[b'sealed'] not in (b'1', b'2'):
                raise Conflict('all shards must be sealed before bounding')
            rows.append(row)
        self.registry.record_bound(grant, tuple(rows))

    def rotate(self, previous: Grant, epoch: str, settings: Settings, *, tier: int,
               sizing: Sizing) -> Operation:
        self.registry.retire(previous)
        # Include ALL older unclosed epochs, not just the direct predecessor.
        for grant in self.registry.grants():
            if grant.workspace != previous.workspace or self.registry.retention_deadline(grant) is not None:
                continue
            if not self.registry.is_retired(grant):
                continue  # A concurrent successor is arbitrated by the registry.
            for shard in range(grant.shards):
                yield from self.seal_shard(grant, shard)
            yield from self.bound_epoch(grant)
        return self.registry.successor(previous, epoch, settings, tier=tier, sizing=sizing)

    def purge(self, ticket: Ticket, *, now: int) -> Operation:
        deadline = self.registry.retention_deadline(ticket.grant)
        if deadline is None or now < max(deadline, ticket.idempotency_until):
            raise Conflict('epoch not closed or idempotency window still open')
        key = ticket.grant.row_key(ticket.shard)
        row = yield Read(key, (*META, ticket.column))
        validate_metadata(row, ticket.grant, ticket.shard)
        if ticket.column not in row:
            return
        state = slot_state(row, ticket)
        if not state['ack'] or row[b'sealed'] not in (b'1', b'2'):
            raise Conflict('unresolved intent cannot be deleted by age')
        # Keep the sealed row marker permanently. Tickets/epoch IDs cannot be
        # reused; subsequent old retries fail closed, never re-create a slot.
        yield Compare(key, ticket.column, row[ticket.column], {
            ticket.column: None, b'sealed': b'2', b'version': token(),
        })
