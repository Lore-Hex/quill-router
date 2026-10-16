"""Finite reference-derived export plans. No discovery, storage client or retries."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

from trusted_router.async_settle_shadow_v2_gate_inputs import Key, Row
from trusted_router.async_settle_shadow_v2_schema import require


@dataclass(frozen=True)
class KeyRange:
    kind: str
    start: str
    end: str
    limit: int


class Reader(Protocol):
    def get(self, key: Key) -> bytes | None: ...
    def read_range(self, bounds: KeyRange) -> tuple[Row, ...]: ...


@dataclass
class MemoryReader:
    rows: tuple[Row, ...]

    def get(self, key: Key) -> bytes | None:
        values = [r.raw for r in self.rows if r.key == key]
        require(len(values) <= 1, 'artifact_gap')
        return values[0] if values else None

    def read_range(self, bounds: KeyRange) -> tuple[Row, ...]:
        require(0 < bounds.limit <= 200 and bounds.start < bounds.end, 'evidence_export_budget_gap')
        found = tuple(sorted((r for r in self.rows if r.key.kind == bounds.kind
                              and bounds.start <= r.key.id < bounds.end), key=lambda r: r.key))
        require(len(found) <= bounds.limit, 'evidence_export_budget_gap')
        require(len({r.key for r in found}) == len(found), 'artifact_gap')
        return found


@dataclass(frozen=True)
class Lane:
    active_us: int
    operations: int
    bytes: int
    inflight_at_start: int


@dataclass(frozen=True)
class ExportPlan:
    keys: tuple[Key, ...]
    ranges: tuple[KeyRange, ...]
    writes: tuple[Key, ...]
    source_gets: int
    operations: int
    actual_bytes: int
    coarse_bytes: int
    passes: int
    lane_operation_bound: int
    lane_bytes_bound: int
    cleanup_spanner_transactions: int
    cleanup_sink_keys: int
    read_timeout_ms: int = 200
    priority: str = 'LOW'
    retries: int = 0


def plan(keys: tuple[Key, ...], ranges: tuple[KeyRange, ...], writes: tuple[Key, ...],
         *, source_bytes: int, write_bytes: int, passes: int, lanes: tuple[Lane, ...],
         cleanup_spanner_keys: int = 0, cleanup_sink_keys: int = 0,
         cleanup_seconds: int = 30) -> ExportPlan:
    """Inputs must originate in validated manifests, inventory or references."""
    require(len(keys) == len(set(keys)) and len(writes) == len(set(writes)), 'artifact_gap')
    require(len(keys) + sum(r.limit for r in ranges) <= 10000, 'evidence_export_budget_gap')
    require(type(passes) is int and passes >= 1, 'evidence_export_budget_gap')
    for r in ranges:
        require(r.start < r.end and 0 < r.limit <= 200, 'evidence_export_budget_gap')
        require(not any(k.kind == r.kind and r.start <= k.id < r.end for k in keys), 'artifact_gap')
    ordered = sorted(ranges, key=lambda r: (r.kind, r.start))
    require(all(a.kind != b.kind or a.end <= b.start for a, b in zip(ordered, ordered[1:], strict=False)), 'artifact_gap')
    gets = len(keys) + len(ranges)
    operations = (gets + len(writes)) * passes
    require(0 <= source_bytes <= 262144 * gets and 0 <= write_bytes <= 262144 * len(writes), 'evidence_export_budget_gap')
    bound = 0
    for lane in lanes:
        require(lane.active_us >= 0 and lane.inflight_at_start in (0, 1), 'evidence_export_budget_gap')
        maximum = 1 + lane.active_us // 5000000 + lane.inflight_at_start
        require(0 <= lane.operations <= maximum and 0 <= lane.bytes <= 65536 * lane.operations, 'evidence_export_budget_gap')
        bound += maximum
    transactions = math.ceil(cleanup_spanner_keys / 200)
    require(0 <= cleanup_seconds <= 30 and 0 <= cleanup_spanner_keys
            and transactions <= 5 * cleanup_seconds
            and 0 <= cleanup_sink_keys <= 1000 * cleanup_seconds, 'evidence_export_budget_gap')
    return ExportPlan(tuple(sorted(keys)), tuple(ordered), tuple(sorted(writes)), gets,
                      operations, (source_bytes + write_bytes) * passes, 262144 * operations,
                      passes, bound, 65536 * bound, transactions, cleanup_sink_keys)


def execute(reader: Reader, export: ExportPlan) -> tuple[Row, ...]:
    """Read each planned source once, retaining absence as a failed contract."""
    rows = []
    for key in export.keys:
        raw = reader.get(key)
        require(raw is not None, 'artifact_gap')
        assert raw is not None
        rows.append(Row(key, raw))
    for bounds in export.ranges:
        found = reader.read_range(bounds)
        require(len(found) <= bounds.limit and all(r.key.kind == bounds.kind
                and bounds.start <= r.key.id < bounds.end for r in found), 'artifact_gap')
        rows.extend(found)
    require(len({r.key for r in rows}) == len(rows), 'artifact_gap')
    require(sum(len(r.raw) for r in rows) <= export.actual_bytes, 'evidence_export_budget_gap')
    return tuple(rows)


def derive(ctx: object, *, writes: tuple[Key, ...] = (), passes: int = 1,
           lanes: tuple[Lane, ...] = ()) -> ExportPlan:
    """Derive sources from validated inventory, manifests and receipt references.

    Ranges retain the issuer's forward horizon across every inventoried router;
    no enclave continuity, writer closure or found-object listing discharges it.
    """
    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router import async_settle_shadow_v2_a1_reconciliation as joins
    from trusted_router import async_settle_shadow_v2_evidence as p1
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context
    from trusted_router.async_settle_shadow_v2_schema import day_at, sha256
    from trusted_router.detached_jws import canonical

    require(isinstance(ctx, Context), 'artifact_gap')
    assert isinstance(ctx, Context)
    require(ctx.bundle.inventory is not None and ctx.proof is not None, 'artifact_gap')
    assert ctx.bundle.inventory is not None and ctx.proof is not None
    proof_raw = canonical(ctx.proof)
    sources = {r.key: r.raw for r in (*ctx.bundle.rows, *ctx.bundle.attachments)}
    sources[Key('proof_manifest', sha256(ctx.proof))] = proof_raw
    for ledger, raw in zip(ctx.ledgers, ctx.bundle.ledgers, strict=True):
        sources[Key('prerequisite_ledger', sha256(ledger))] = raw
    import hashlib
    for preimage in ctx.bundle.preimages:
        sources[Key('artifact', hashlib.sha256(preimage.raw).hexdigest())] = preimage.raw
    keys: set[Key] = {k for k in sources if k.kind in ('proof_manifest', 'prerequisite_ledger', 'artifact')}
    for ref in ctx.proof['attachments']:
        matches = [k for k, raw in sources.items() if hashlib.sha256(raw).hexdigest() == ref['sha256']]
        require(len(matches) == 1, 'artifact_gap')
        keys.add(matches[0])
    ranges: set[KeyRange] = set()
    for day in ctx.bundle.days:
        keys.add(Key(p1.KINDS[5], day + '/manifest-v2'))
        for boot in ctx.bundle.inventory.boots:
            if boot.role == 'router' and day_at(boot.start_us) <= day and (boot.end_us is None or day_at(boot.end_us - 1) >= day):
                keys.update(Key(kind, day + '/' + boot.boot) for kind in (p1.KINDS[3], p1.KINDS[4], a1.KINDS[-1]))
    for page, event in ctx.events():
        require(any(b.boot == page['boot'] and b.role == 'router'
            and b.start_us <= event['event_at_us'] < (b.end_us or ctx.bundle.completed_at_us)
            for b in ctx.bundle.inventory.boots), 'writer_roster_gap')
        keys.add(Key(p1.KINDS[2], '/'.join(page[k] for k in ('day', 'boot', 'first_seq'))))
        if event['phase'] in ('settle', 'refund'):
            if event['proof_kind'] != 'none':
                kind = p1.KINDS[1] if event['proof_kind'] == 'certificate' else p1.KINDS[0]
                keys.add(Key(kind, event['authorization_day'] + '/' + event['authorization_id']))
        original = event.get('original_receipt')
        if original is not None:
            keys.add(Key(p1.KINDS[2], '/'.join(original[k] for k in ('day', 'boot', 'first_seq'))))
        if event['phase'] == 'authorize':
            obligation = joins.authorization_obligation(page, event['seq'])
            low_day, high_day = page['day'], day_at(obligation['horizon_at_us'] - 1)
            reconciled = False
            for terminal_page, terminal in ctx.events():
                if terminal['phase'] not in ('settle', 'refund') or terminal['authorization_id'] != event['authorization_id']:
                    continue
                try:
                    joins.validate_terminal_join(obligation, page, terminal_page,
                        joins.event_locator(terminal_page, terminal['seq']))
                    reconciled = True
                except ValueError:
                    continue
            if not reconciled:
                # Open obligations require the full independent forward inventory.
                require(any(b.end_us is not None and day_at(b.end_us - 1) >= high_day
                    for b in ctx.bundle.inventory.boots), 'authorization_horizon_gap')
            for counter in ctx.kind(p1.KINDS[3]):
                if low_day <= counter['day'] <= high_day and counter['event_seq']:
                    prefix = counter['day'] + '/' + counter['boot'] + '/'
                    for seq in range(1, counter['event_seq'] + 1, 200):
                        ranges.add(KeyRange(p1.KINDS[2], prefix + f'{seq:020}',
                            prefix + f'{min(seq + 200, counter["event_seq"] + 1):020}', min(200, counter['event_seq'] - seq + 1)))
    # Coverage enumerates complete attachment keys, including absent uploads.
    for page in ctx.attached('coverage'):
        stream = page['stream']
        for entry in page['entries']:
            keys.add(Key(a1.KINDS[-1] if stream == 'close_result' else stream, entry['key']))
    for key in ctx.proof['explicit_keys']:
        matches = [r.key for r in ctx.bundle.rows if r.key.id == key]
        require(bool(matches), 'artifact_gap')
        keys.update(matches)
    # Exact point reads covered by a bounded range are fetched only once.
    keys = {k for k in keys if not any(r.kind == k.kind and r.start <= k.id < r.end for r in ranges)}
    body_bytes = sum(len(raw) for key, raw in sources.items() if key in keys or any(r.kind == key.kind and r.start <= key.id < r.end for r in ranges))
    return plan(tuple(keys), tuple(ranges), writes, source_bytes=body_bytes, write_bytes=0, passes=passes, lanes=lanes)
