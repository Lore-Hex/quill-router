"""Durable journal registry. Only admission/recovery use this adapter.

No route constructs it yet. Snapshot hashes are inputs, independent of the
legacy authorization pricing fields. All workspace transitions serialize on
workspace-key ranges; authorization tombstones are never deleted or recycled.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from trusted_router.config import Settings
from trusted_router.settlement_journal import (
    META,
    PAGE_SLOTS,
    Conflict,
    Grant,
    Read,
    ReadPage,
    Receipt,
    RetryLater,
    Sizing,
    Ticket,
    slot_state,
    validate_metadata,
)
from trusted_router.trust_eligibility import tier_cap

# Both correlated predicates and the transaction interlock use the complete PK.
ASYNC_GUARD_COUNT_SQL = (
    "SELECT COUNT(*) FROM tr_async_settlement_obligation "
    "WHERE authorization_id=@aid AND state NOT IN ('acknowledged', 'fenced')"
)

OBLIGATION_ABSENT_CACHE_SECONDS = 5.0
_OBLIGATION_AVAILABILITY_CACHE: dict[str, tuple[bool, float]] = {}
_OBLIGATION_AVAILABILITY_LOCK = threading.Lock()


def obligation_table_available(database: Any, param_types: Any) -> bool:
    """Probe once per database/process; retry confirmed absence after five seconds.

    Only a missing table permits pre-migration SQL. Other probe failures select
    guarded SQL without caching. Presence is permanent, independent of admission
    settings. Separate state from the outbox: either migration can land first.
    Nameless test doubles are intentionally uncached (object IDs are reusable).
    """
    # Local import avoids the authorize -> registry -> authorize import cycle.
    from trusted_router.storage_gcp_authorize import _is_table_missing

    name = getattr(database, "name", None)
    key = name if isinstance(name, str) and name else None
    with _OBLIGATION_AVAILABILITY_LOCK:
        now = time.monotonic()
        cached = _OBLIGATION_AVAILABILITY_CACHE.get(key) if key is not None else None
        if cached is not None and cached[1] > now:
            return cached[0]
        try:
            with database.snapshot() as snapshot:
                list(snapshot.execute_sql(
                    ASYNC_GUARD_COUNT_SQL, params={"aid": ""},
                    param_types={"aid": param_types.STRING},
                ))
        except Exception as exc:
            if not _is_table_missing(exc, "tr_async_settlement_obligation"):
                return True
            available = False
        else:
            available = True
        if key is not None:
            _OBLIGATION_AVAILABILITY_CACHE[key] = (
                available, float("inf") if available else now + OBLIGATION_ABSENT_CACHE_SECONDS,
            )
        return available

_GRANTS = (
    "SELECT workspace_id, epoch, region, cap, shards, slots, state, recorded_bound, "
    "successor_epoch, retention_deadline, created_at, updated_at "
    "FROM tr_async_settlement_budget"
)
_OBLIGATIONS = (
    "SELECT authorization_id, workspace_id, epoch, region, shard, slot, generation_id, "
    "key_id, invocation_nonce, snapshot_hash, snapshot_version, idempotency_deadline, "
    "state, payload_hash, amount, ledger_receipt, created_at, updated_at "
    "FROM tr_async_settlement_obligation"
)
_GRANT_COLUMNS = (
    'workspace_id', 'epoch', 'region', 'cap', 'shards', 'slots', 'state',
    'recorded_bound', 'successor_epoch', 'retention_deadline', 'created_at', 'updated_at',
)
_OBLIGATION_COLUMNS = (
    'authorization_id', 'workspace_id', 'epoch', 'region', 'shard', 'slot',
    'generation_id', 'key_id', 'invocation_nonce', 'snapshot_hash', 'snapshot_version',
    'idempotency_deadline', 'state', 'payload_hash', 'amount', 'ledger_receipt',
    'created_at', 'updated_at',
)


def _grant(row: dict[str, Any]) -> Grant:
    return Grant(row['workspace_id'], row['region'], row['epoch'], int(row['cap']),
                 int(row['shards']), int(row['slots']))


def _ticket(row: dict[str, Any], grant: Grant) -> Ticket:
    return Ticket(grant, int(row['shard']), int(row['slot']), row['authorization_id'],
                  row['generation_id'], row['key_id'], row['invocation_nonce'],
                  row['snapshot_hash'], int(row['idempotency_deadline']),
                  int(row['snapshot_version']))


class SpannerEpochRegistry:
    def __init__(self, database: Any, param_types: Any, *, journal_storage: Any = None) -> None:
        self.database = database
        self.pt = param_types
        self.journal_storage = journal_storage

    def _grants(self, reader: Any, workspace: str | None = None) -> list[dict[str, Any]]:
        # Registry operations never fall back to pre-migration behavior. This
        # also protects grant-only calls against a partially applied migration.
        list(reader.execute_sql(ASYNC_GUARD_COUNT_SQL, params={"aid": ""},
                                param_types={"aid": self.pt.STRING}))
        sql = _GRANTS
        params = {}
        if workspace is not None:
            sql += ' WHERE workspace_id=@workspace'
            params['workspace'] = workspace
        return [dict(zip(_GRANT_COLUMNS, row, strict=True)) for row in reader.execute_sql(
            sql, params=params, param_types={k: self.pt.STRING for k in params})]

    def _obligations(self, reader: Any, *, aid: str | None = None,
                     grant: Grant | None = None) -> list[dict[str, Any]]:
        sql = _OBLIGATIONS
        if aid is not None:
            sql += ' WHERE authorization_id=@aid'
            params = {'aid': aid}
        else:
            assert grant is not None
            sql += ' WHERE workspace_id=@workspace AND epoch=@epoch'
            params = {'workspace': grant.workspace, 'epoch': grant.epoch}
        return [dict(zip(_OBLIGATION_COLUMNS, row, strict=True)) for row in reader.execute_sql(
            sql, params=params, param_types={k: self.pt.STRING for k in params})]

    @staticmethod
    def _write(tx: Any, table: str, row: dict[str, Any]) -> None:
        # Mutations only: no DML mixed into registry transactions.
        tx.insert_or_update(table=table, columns=tuple(row), values=[tuple(row.values())])

    def _put_grant(self, tx: Any, row: dict[str, Any]) -> None:
        self._write(tx, 'tr_async_settlement_budget', {**row, 'updated_at': datetime.now(UTC)})

    @staticmethod
    def _new_grant(grant: Grant) -> dict[str, Any]:
        return dict(workspace_id=grant.workspace, epoch=grant.epoch, region=grant.region,
                    cap=grant.cap, shards=grant.shards, slots=grant.slots, state='active',
                    recorded_bound=None, successor_epoch=None, retention_deadline=None,
                    created_at=datetime.now(UTC), updated_at=datetime.now(UTC))

    def _require(self, reader: Any, grant: Grant) -> dict[str, Any]:
        for row in self._grants(reader, grant.workspace):
            if row['epoch'] == grant.epoch and _grant(row) == grant:
                return row
        raise Conflict('unknown or changed grant')

    def register(self, grant: Grant, settings: Settings, *, tier: int | None) -> None:
        if type(tier) is not int or tier not in (1, 2, 3) or grant.cap > tier_cap(settings, tier):
            raise Conflict('workspace is ineligible or grant exceeds trust cap')
        if grant.cap // grant.shards < settings.async_settlement_journal_minimum_shard_cap_micros:
            raise Conflict('grant below minimum shard capacity')
        def txn(tx: Any) -> None:
            rows = self._grants(tx, grant.workspace)
            if any(_grant(r) == grant for r in rows):
                return
            if any(r['state'] != 'closed' or r['epoch'] == grant.epoch for r in rows):
                raise Conflict('workspace has an open epoch or epoch was recycled')
            self._put_grant(tx, self._new_grant(grant))
        self.database.run_in_transaction(txn)

    def _bind(self, tx: Any, ticket: Ticket, rows: list[dict[str, Any]]) -> None:
        old = self._obligations(tx, aid=ticket.authorization)
        if old:
            if (_ticket(old[0], ticket.grant) != ticket or
                    (old[0]['workspace_id'], old[0]['region'], old[0]['epoch']) !=
                    (ticket.grant.workspace, ticket.grant.region, ticket.grant.epoch)):
                raise Conflict('authorization binding is immutable')
            return
        if self._require(tx, ticket.grant)['state'] != 'active':
            raise Conflict('epoch is not active')
        if any(int(r['shard']) == ticket.shard and int(r['slot']) == ticket.slot for r in rows):
            raise Conflict('slot is already allocated')
        # MF2 inverse interlock: if a reaper already won, no guard/ticket can be
        # issued. Reads include absent rows and serialize against insertion.
        terminal = list(tx.execute_sql(
            "SELECT settled FROM tr_gateway_authorization WHERE authorization_id=@aid",
            params={'aid': ticket.authorization}, param_types={'aid': self.pt.STRING}))
        reservations = list(tx.execute_sql(
            "SELECT settled FROM tr_reservation@{FORCE_INDEX=tr_reservation_by_authorization} "
            "WHERE authorization_id=@aid",
            params={'aid': ticket.authorization}, param_types={'aid': self.pt.STRING}))
        if any(r[0] for r in [*terminal, *reservations]):
            raise Conflict('reaper or finalizer already won')
        row = dict(zip(_OBLIGATION_COLUMNS, (
            ticket.authorization, ticket.grant.workspace, ticket.grant.epoch, ticket.grant.region,
            ticket.shard, ticket.slot, ticket.generation, ticket.key_id, ticket.nonce,
            ticket.snapshot_hash, ticket.snapshot_version, ticket.idempotency_until,
            'pending', None, None, None, datetime.now(UTC), datetime.now(UTC),
        ), strict=True))
        self._write(tx, 'tr_async_settlement_obligation', row)

    def bind(self, ticket: Ticket) -> None:
        def txn(tx: Any) -> None:
            self._require(tx, ticket.grant)
            self._bind(tx, ticket, self._obligations(tx, grant=ticket.grant))
        self.database.run_in_transaction(txn)

    def allocate(self, grant: Grant, *, authorization: str, generation: str,
                 key_id: str, nonce: str, snapshot_hash: str, idempotency_until: int) -> Ticket:
        shard = int.from_bytes(hashlib.sha256(authorization.encode()).digest()[:8]) % grant.shards
        def txn(tx: Any) -> Ticket:
            self._require(tx, grant)
            old = self._obligations(tx, aid=authorization)
            rows = self._obligations(tx, grant=grant)
            used = {int(r['slot']) for r in rows if int(r['shard']) == shard}
            slot = int(old[0]['slot']) if old else next((i for i in range(grant.slots) if i not in used), None)
            if slot is None:
                raise Conflict('shard slots exhausted; synchronous authorization required')
            ticket = Ticket(grant, shard, slot, authorization, generation, key_id, nonce,
                            snapshot_hash, idempotency_until)
            self._bind(tx, ticket, rows)
            return ticket
        return self.database.run_in_transaction(txn)

    def grants(self) -> tuple[Grant, ...]:
        with self.database.snapshot(multi_use=True) as reader:
            return tuple(_grant(r) for r in self._grants(reader))

    def tickets(self, grant: Grant) -> tuple[Ticket, ...]:
        with self.database.snapshot(multi_use=True) as reader:
            self._require(reader, grant)
            return tuple(_ticket(r, grant) for r in self._obligations(reader, grant=grant))

    def registered(self, ticket: Ticket) -> bool:
        with self.database.snapshot(multi_use=True) as reader:
            rows = self._obligations(reader, aid=ticket.authorization)
            return bool(rows and _ticket(rows[0], ticket.grant) == ticket and
                        (rows[0]['workspace_id'], rows[0]['region'], rows[0]['epoch']) ==
                        (ticket.grant.workspace, ticket.grant.region, ticket.grant.epoch) and
                        any(_grant(r) == ticket.grant for r in self._grants(reader, ticket.grant.workspace)))

    def retire(self, grant: Grant) -> None:
        def txn(tx: Any) -> None:
            row = self._require(tx, grant)
            if row['state'] != 'closed':
                self._put_grant(tx, {**row, 'state': 'retiring'})
        self.database.run_in_transaction(txn)

    def is_retired(self, grant: Grant) -> bool:
        with self.database.snapshot(multi_use=True) as reader:
            return any(_grant(r) == grant and r['state'] in ('retiring', 'closed')
                       for r in self._grants(reader, grant.workspace))

    def retention_deadline(self, grant: Grant) -> int | None:
        with self.database.snapshot(multi_use=True) as reader:
            value = self._require(reader, grant)['retention_deadline']
            return None if value is None else int(value)

    def record_bound(self, grant: Grant, rows: tuple[dict[bytes, bytes], ...]) -> None:
        if len(rows) != grant.shards:
            raise Conflict('bound requires every retired shard')
        bound = 0
        for shard, evidence in enumerate(rows):
            validate_metadata(evidence, grant, shard)
            if evidence[b'sealed'] not in (b'1', b'2'):
                raise Conflict('bound requires every shard sealed')
            bound += int(evidence[b'outstanding'])
        def txn(tx: Any) -> None:
            row = self._require(tx, grant)
            if row['state'] == 'active':
                raise Conflict('retire before bounding')
            old = row['recorded_bound']
            self._put_grant(tx, {**row, 'recorded_bound': min(bound, grant.cap if old is None else int(old))})
        self.database.run_in_transaction(txn)

    def successor(self, previous: Grant, epoch: str, settings: Settings, *, tier: int,
                  sizing: Sizing) -> Grant:
        if type(tier) is not int or tier not in (1, 2, 3):
            raise Conflict('workspace is ineligible')
        def txn(tx: Any) -> Grant:
            rows = self._grants(tx, previous.workspace)
            prior = next((r for r in rows if _grant(r) == previous), None)
            if prior is None or prior['state'] == 'active':
                raise Conflict('retire before successor')
            if prior['successor_epoch'] is not None:
                old = next(_grant(r) for r in rows if r['epoch'] == prior['successor_epoch'])
                if (old.epoch, old.shards, old.slots) != (epoch, sizing.shards, sizing.slots):
                    raise Conflict('successor transition is immutable')
                return old
            opened = [r for r in rows if r['state'] != 'closed']
            if any(r['state'] == 'active' or r['recorded_bound'] is None for r in opened):
                raise Conflict('all older epochs require sealed bounds')
            remaining = tier_cap(settings, tier) - sum(int(r['recorded_bound']) for r in opened)
            if remaining <= 0 or remaining // sizing.shards < settings.async_settlement_journal_minimum_shard_cap_micros:
                raise RetryLater('insufficient remaining capacity')
            grant = Grant(previous.workspace, previous.region, epoch, remaining, sizing.shards, sizing.slots)
            if any(r['epoch'] == epoch for r in rows):
                raise Conflict('epoch cannot be recycled')
            self._put_grant(tx, self._new_grant(grant))
            self._put_grant(tx, {**prior, 'successor_epoch': epoch})
            return grant
        return self.database.run_in_transaction(txn)

    def reconcile(self, ticket: Ticket) -> None:
        """Persist a terminal journal decision from the pinned recovery reader.

        Fenced means the irrevocable journal CAS won. A pending, inaccessible or
        missing journal row never clears a guard. Receipts remain ledger-owned.
        """
        if self.journal_storage is None:
            raise Conflict('reconciliation requires pinned journal evidence')
        evidence = self.journal_storage.call(Read(
            ticket.grant.row_key(ticket.shard), (*META, ticket.column)))
        validate_metadata(evidence, ticket.grant, ticket.shard)
        state = slot_state(evidence, ticket)
        if state['status'] == 'pending':
            return
        def txn(tx: Any) -> None:
            self._require(tx, ticket.grant)
            rows = self._obligations(tx, aid=ticket.authorization)
            if (not rows or _ticket(rows[0], ticket.grant) != ticket or
                    (rows[0]['workspace_id'], rows[0]['region'], rows[0]['epoch']) !=
                    (ticket.grant.workspace, ticket.grant.region, ticket.grant.epoch)):
                raise Conflict('unknown binding')
            self._project(tx, rows[0], state)
        self.database.run_in_transaction(txn)

    def _project(self, tx: Any, row: dict[str, Any], state: dict[str, Any]) -> None:
        payload_hash = state.get('hash', '')
        amount = int(state.get('envelope', {}).get('charge', 0))
        if row['state'] != 'pending' and (row['payload_hash'], row['amount']) != (payload_hash, amount):
            raise Conflict('terminal payload is immutable')
        status = 'acknowledged' if state['ack'] else state['status']
        if row['state'] == 'acknowledged':
            return
        self._write(tx, 'tr_async_settlement_obligation', {
            **row, 'state': status, 'payload_hash': payload_hash, 'amount': amount,
            'updated_at': datetime.now(UTC),
        })

    def close(self, grant: Grant, *, retain_until: int) -> None:
        if type(retain_until) is not int or not 0 <= retain_until < 2**63:
            raise Conflict('invalid retention deadline')
        # Read immutable acknowledged slots before entering the retryable Spanner
        # transaction. A closed epoch can already be purged: replay needs no slots.
        with self.database.snapshot(multi_use=True) as reader:
            row = self._require(reader, grant)
            tickets = [_ticket(r, grant) for r in self._obligations(reader, grant=grant)]
        if row['state'] == 'active':
            raise Conflict('retire before close')
        verified: dict[Ticket, dict[str, Any]] = {}
        if row['state'] != 'closed':
            if any(t.idempotency_until > retain_until for t in tickets):
                raise Conflict('retention deadline precedes idempotency window')
            if self.journal_storage is None:
                raise Conflict('closure requires pinned journal evidence')
            for shard in range(grant.shards):
                evidence = self.journal_storage.call(Read(grant.row_key(shard), META))
                validate_metadata(evidence, grant, shard)
                if evidence[b'sealed'] != b'1' or int(evidence[b'outstanding']) != 0:
                    raise Conflict('unsealed shard or outstanding debt')
                for start in range(0, grant.slots, PAGE_SLOTS):
                    stop = min(start + PAGE_SLOTS, grant.slots)
                    page = self.journal_storage.call(ReadPage(grant.row_key(shard), start, stop))
                    validate_metadata(page, grant, shard)
                    expected = {t.column: t for t in tickets if t.shard == shard and start <= t.slot < stop}
                    if {c for c in page if c.startswith(b's/')} != set(expected):
                        raise Conflict('missing or unregistered closure slot')
                    if any(not slot_state(page, t)['ack'] for t in expected.values()):
                        raise Conflict('unacknowledged slot')
                    verified.update((t, slot_state(page, t)) for t in expected.values())
        def txn(tx: Any) -> None:
            row = self._require(tx, grant)
            if row['state'] == 'active':
                raise Conflict('retire before close')
            obligations = self._obligations(tx, grant=grant)
            if row['state'] != 'closed':
                if {_ticket(r, grant) for r in obligations} != set(verified):
                    raise Conflict('closure bindings changed')
                for obligation in obligations:
                    self._project(tx, obligation, verified[_ticket(obligation, grant)])
            elif any(r['state'] not in ('acknowledged', 'fenced') for r in obligations):
                raise Conflict('closed epoch has unresolved obligations')
            # Projections and the purge-enabling deadline commit together. A
            # crash or aborted transaction leaves both the debt guard and slots.
            self._put_grant(tx, {**row, 'state': 'closed', 'recorded_bound': 0,
                                'retention_deadline': max(retain_until, int(row['retention_deadline'] or 0))})
        self.database.run_in_transaction(txn)


class SpannerReceiptVerifier:
    """Read only ledger receipts persisted by the ledger transaction (PR 7).

    Import receipts have no representation here. This PR intentionally exposes
    no standalone receipt minting method: a worker cannot assert finalization.
    """
    def __init__(self, registry: SpannerEpochRegistry) -> None:
        self.registry = registry

    def verify(self, ticket: Ticket, receipt: Receipt) -> bool:
        if receipt.authorization != ticket.authorization or receipt.epoch != ticket.grant.epoch:
            return False
        with self.registry.database.snapshot(multi_use=True) as reader:
            rows = self.registry._obligations(reader, aid=ticket.authorization)
            grants = self.registry._grants(reader, ticket.grant.workspace)
            if not rows or not any(_grant(r) == ticket.grant for r in grants):
                return False
            row = rows[0]
            return bool(
                (row['workspace_id'], row['region'], row['epoch']) ==
                (ticket.grant.workspace, ticket.grant.region, ticket.grant.epoch) and
                _ticket(row, ticket.grant) == ticket and
                row['state'] in ('accepted', 'sync_required', 'fenced', 'acknowledged') and
                row['payload_hash'] == receipt.payload_hash and
                row['ledger_receipt'] == json.dumps({
                    'kind': 'ledger_finalization', 'ticket': asdict(ticket),
                    'receipt': asdict(receipt), 'amount': row['amount'],
                }, sort_keys=True, separators=(',', ':'))
            )
