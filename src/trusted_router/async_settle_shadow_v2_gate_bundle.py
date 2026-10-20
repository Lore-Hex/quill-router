"""One original-byte decoding and identity boundary for all gate rules."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router import async_settle_shadow_v2_a1_reconciliation as reconciliation
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Key, Ports
from trusted_router.async_settle_shadow_v2_gate_loading import error_detail
from trusted_router.async_settle_shadow_v2_gate_result import Subject
from trusted_router.async_settle_shadow_v2_schema import decode, require, sha256

ATTACHMENTS = {
    'shutdown_entry': a2.SHUTDOWN_ENTRY,
    'shutdown_measurement': a2.SHUTDOWN_MEASUREMENT,
    'health_basis': health.HEALTH_BASIS_BATCH,
    'publisher_trace': health.PUBLISHER_TRACE_PAGE,
    'handoff_accounting': a1.HANDOFF_ACCOUNTING,
    'authorization_obligation': reconciliation.AUTHORIZATION_OBLIGATION_PAGE,
    'coverage': a1.COVERAGE_PAGE,
}


@dataclass(frozen=True)
class Issue:
    gap: str
    subject: Subject
    detail: str
    object_ids: tuple[str, ...] = ()
    count: int = 1
    unavailable: bool = False


@dataclass
class Context:
    bundle: Bundle
    ports: Ports | None
    gate: str
    rows: dict[Key, dict[str, Any]] = field(default_factory=dict)
    attachments: dict[Key, dict[str, Any]] = field(default_factory=dict)
    proof: dict[str, Any] | None = None
    ledgers: list[dict[str, Any]] = field(default_factory=list)
    gates: dict[str, dict[str, Any]] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    verifier_calls: dict[str, int] = field(default_factory=dict)
    verified: set[str] = field(default_factory=set)
    obligations: list[dict[str, Any]] = field(default_factory=list)
    salvaged_events: list[tuple[dict[str, Any], dict[str, Any]]] = field(default_factory=list)
    release_files: dict[str, bytes] = field(default_factory=dict)
    reset_history: list[dict[str, Any]] = field(default_factory=list)
    release_artifacts_cache: tuple[Any, ...] | None = None
    certified_start_us: int | None = None
    certified_end_us: int | None = None
    trace_population_cache: tuple[Any, frozenset[str]] | None = None

    def kind(self, kind: str) -> list[dict[str, Any]]:
        return [v for k, v in self.rows.items() if k.kind == kind]

    def manifests(self) -> list[dict[str, Any]]:
        from trusted_router.async_settle_shadow_v2_gate_controls import manifests
        return manifests(self.rows)

    def attached(self, kind: str) -> list[dict[str, Any]]:
        return [v for k, v in self.attachments.items() if k.kind == kind]

    def events(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return [(page, event) for page in self.kind(p1.KINDS[2]) for event in page['events']] + self.salvaged_events

    def issue(self, gap: str, detail: str, subject: Subject = Subject(),
              ids: tuple[str, ...] = (), *, count: int | None = None, unavailable: bool = False) -> None:
        a2.GAP_NAME(gap)
        self.issues.append(Issue(gap, subject, detail, tuple(sorted(ids)), (len(ids) or 1) if count is None else count, unavailable))


def subject(value: dict[str, Any], object_id: str | None = None) -> Subject:
    return Subject(day=value.get('day'), boot=value.get('boot'), object_id=object_id)


def decode_bundle(bundle: Bundle, ports: Ports | None, gate: str) -> Context:
    ctx = Context(bundle, ports, gate)
    all_rows = (*bundle.rows, *bundle.attachments)
    seen: dict[Key, bytes] = {}
    for row in all_rows:
        try:
            sub = Subject(object_id=row.key.id, wire=a2.O(row.key.kind, row.key.id))
        except ValueError:
            ctx.issue('evidence_schema_gap', 'unencodable expected row identity')
            sub = Subject(wire=a2.N())
        if row.key in seen:
            if seen[row.key] == row.raw:
                continue
            ctx.rows.pop(row.key, None)
            ctx.attachments.pop(row.key, None)
            gap = 'close_result_conflict' if row.key.kind == a1.KINDS[-1] else 'artifact_gap'
            ctx.issue(gap, 'duplicate complete key', sub,
                      (hashlib.sha256(seen[row.key]).hexdigest(), hashlib.sha256(row.raw).hexdigest()))
            continue
        seen[row.key] = row.raw
        try:
            if row.key.kind in ATTACHMENTS:
                value = ATTACHMENTS[row.key.kind].parse(row.raw)
                ctx.attachments[row.key] = value
            else:
                ctx.rows[row.key] = a1.validate_row(row.key.kind, row.key.id, row.raw)
        except ValueError as exc:
            # Tags are inspected only to classify a rejected original row. The
            # contract validator is the sole successful decode/identity route.
            try:
                tags = decode(row.raw, 262144, retained_strings=True)
            except ValueError:
                tags = None
            if type(tags) is dict and (tags.get('policy_version') != 'shadow-v2'
                    or tags.get('v') != 2 or 'contract_revision' in tags and tags['contract_revision'] != 'a1'
                    or row.key.kind in a1.KINDS[3:] and tags.get('contract_revision') != 'a1'):
                ctx.issue('policy_version_gap', 'changed policy or contract revision', sub)
            if row.key.kind != p1.KINDS[2] or not invalid_events(ctx, row.key, tags):
                ctx.issue('evidence_schema_gap', error_detail(exc), sub)
            owner = {p1.KINDS[2]: 'terminal_coverage_gap', p1.KINDS[3]: 'phase_coverage_gap',
                     p1.KINDS[4]: 'writer_closure_gap', a1.KINDS[-1]: 'close_result_identity_gap',
                     'health_basis': 'shadow_health_gap', 'publisher_trace': 'publisher_trace_gap',
                     'authorization_obligation': 'authorization_obligation_gap',
                     'handoff_accounting': 'handoff_coverage_gap'}.get(row.key.kind)
            if owner:
                parts = row.key.id.split('/')
                diagnostic = Subject(parts[0], parts[1], object_id=row.key.id) if len(parts) == 2 else Subject(object_id=row.key.id)
                ctx.issue(owner, 'invalid required row: ' + error_detail(exc), diagnostic)
            if error_detail(exc) == 'a1-publisher-conservation':
                ctx.issue('publisher_accounting_gap', error_detail(exc), diagnostic)
    verified_aliases = {row.key: row.raw for row in bundle.shutdown.aliases} if bundle.shutdown else {}
    supplied_aliases = {row.key: row.raw for row in bundle.attachments if row.key.kind in ('shutdown_entry', 'shutdown_measurement')}
    for key in sorted(verified_aliases.keys() | supplied_aliases.keys()):
        if verified_aliases.get(key) != supplied_aliases.get(key):
            sub = Subject(wire=a2.O(key.kind, key.id))
            ctx.issue('artifact_gap', 'shutdown alias differs from verified population', sub)
            entry = next((entry for entry in bundle.shutdown.entries
                if key.id in ('shutdown/' + entry['day'] + '/' + entry['boot'], entry['measurement']['path'])), None) if bundle.shutdown else None
            shutdown_owner = Subject(day=entry['day'], boot=entry['boot']) if entry else Subject()
            ctx.issue('close_handoff_budget_gap', 'shutdown alias identity or original bytes differ', shutdown_owner)
    if not bundle.rows and gate != 'fleet-budgets-pre':
        ctx.issue('evidence_schema_gap', 'no evidence rows')
    try:
        require(bundle.proof is not None, 'artifact_gap')
        assert bundle.proof is not None
        ctx.proof = a1.PROOF_MANIFEST.parse(bundle.proof)
    except ValueError as exc:
        ctx.issue('artifact_gap', 'proof manifest: ' + error_detail(exc))
    for raw in bundle.ledgers:
        try:
            ctx.ledgers.append(a1.PREREQUISITE_LEDGER.parse(raw))
        except ValueError as exc:
            ctx.issue('boot_prerequisite_gap', error_detail(exc))
    for name in ('transport', 'fleet', 'pre', 'serving'):
        gate_raw = getattr(bundle, name)
        if gate_raw is None:
            from trusted_router.async_settle_shadow_v2_gate_prior import verified_prior_gate
            verified_prior_gate(ctx, name, {})
            continue
        try:
            value = a1.GATE_OUTPUT.parse(gate_raw)
            from trusted_router.async_settle_shadow_v2_gate_prior import verified_prior_gate
            verified_prior_gate(ctx, name, value)
        except ValueError as exc:
            ctx.issue('artifact_gap', name + ': ' + error_detail(exc), Subject(wire=a2.O('prior_gate', name)))
            from trusted_router.async_settle_shadow_v2_gate_prior import verified_prior_gate
            verified_prior_gate(ctx, name, {})
    if ctx.proof is not None:
        raw_by_hash = {hashlib.sha256(row.raw).hexdigest(): row.raw for row in bundle.attachments}
        for item in ctx.proof['attachments']:
            attachment_raw = raw_by_hash.get(item['sha256'])
            if attachment_raw is None or len(attachment_raw) != item['bytes']:
                ctx.issue('artifact_gap', 'missing or changed attachment', Subject(object_id=item['sha256']))
        if not ctx.proof['complete']:
            ctx.issue('artifact_gap', 'incomplete proof')
    for name in bundle.undefined_preimages:
        ctx.issue('artifact_gap', 'undefined preimage: ' + name, Subject(object_id=name))
    if len({p.name for p in bundle.preimages}) != len(bundle.preimages):
        ctx.issue('artifact_gap', 'duplicate supplied preimage')
    from trusted_router.async_settle_shadow_v2_gate_preimages import (
        audit,
        verify_release_file_preimages,
    )
    verify_release_file_preimages(ctx)
    audit(ctx)
    return ctx


def coverage_issues(ctx: Context) -> None:
    raw_rows = {r.key: r.raw for r in (*ctx.bundle.rows, *ctx.bundle.attachments)}
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for page in ctx.attached('coverage'):
        grouped.setdefault((page['day'], page['stream']), []).append(page)
    for day in ctx.bundle.days:
        for stream in a1.COVERAGE_STREAMS:
            if (day, stream) not in grouped:
                ctx.issue('artifact_gap', 'missing expected coverage pages', Subject(day=day, object_id=stream))
    for (day, stream), pages in grouped.items():
        try:
            root = a1.coverage_root(sorted(pages, key=lambda p: p['page_seq']))
            manifest = next((m for m in ctx.manifests() if m.get('day') == day and 'router_writers' in m), None)
            if manifest is not None and stream + '_root' in manifest:
                require(root == manifest[stream + '_root'], 'artifact_gap')
            for page in pages:
                for entry in page['entries']:
                    require(entry['state'] == 'present', 'artifact_gap')
                    raw = raw_rows.get(Key(a1.KINDS[-1] if stream == 'close_result' else stream, entry['key']))
                    require(raw is not None and hashlib.sha256(raw).hexdigest() == entry['sha256']
                            and len(raw) == entry['bytes'], 'artifact_gap')
        except ValueError as exc:
            ctx.issue('artifact_gap', error_detail(exc), Subject(day=day, object_id=stream))


def verify_samples(ctx: Context) -> None:
    from trusted_router.async_settle_shadow_v2_gate_verifiers import check_ports, invoke
    check_ports(ctx)
    if ctx.ports is None:
        ctx.issue('artifact_gap', 'missing mandatory verifier port')
        return
    signatures = {s.object_id: s for s in ctx.bundle.signatures}
    for sample in ctx.kind(p1.KINDS[0]):
        key = sample['authorization_day'] + '/' + sample['authorization_id']
        try:
            from trusted_router.async_settle_shadow_v2_gate_compared import sample_terminal
            sample_terminal(ctx, sample)
            ctx.verified.add(key)
        except ValueError:
            ctx.issue('eligible_coverage_gap', 'retained compared sample join failed', Subject(object_id=key))
    from trusted_router.async_settle_shadow_v2_gate_compared import verify_terminals
    verify_terminals(ctx)
    for page, event in ctx.events():
        if event.get('proof_kind') != 'certificate':
            continue
        key = event['authorization_day'] + '/' + event['authorization_id']
        sig = signatures.get(key)
        try:
            require(sig is not None, 'artifact_gap')
            assert sig is not None
            locator = event['original_receipt']
            require(locator is not None, 'exclusion_proof_gap')
            original_key = Key(p1.KINDS[2], '/'.join(locator[k] for k in ('day', 'boot', 'first_seq')))
            original = next((r.raw for r in ctx.bundle.rows if r.key == original_key), None)
            backing = ctx.ports.verify_original_receipt(sig.claims, returned_key=(original_key.kind, original_key.id), page=original)
            result = invoke(ctx, 'certificate', sig.token, sig.keys, sig.expected, event['receipt_at_us'] // 1000000,
                returned_key=(original_key.kind, original_key.id), page=original,
                attempt=sig.attempt, server_attempt=sig.server_attempt, booking=event['booking']['outcome'])
            require(backing.partition == result.partition == 'B', result.reason or backing.reason or 'exclusion_proof_gap')
            require(sha256(sig.claims['original_receipt']) == sha256(locator), 'exclusion_proof_gap')
            ctx.verified.add(key)
        except ValueError as exc:
            ctx.issue('exclusion_conflict' if error_detail(exc) == 'exclusion_conflict' else 'exclusion_proof_gap',
                      error_detail(exc), subject(page, event['seq']))


def invalid_events(ctx: Context, key: Key, value: Any) -> bool:
    """Separate a bad child from its independently verified page container.

    No invalid child supplies identity, including its claimed event sequence.
    The return value suppresses only recursive container rejection caused by
    children; independently invalid header/coordinates still fail at O.
    """
    try:
        require(type(value) is dict and set(value) == set(p1.PAGE.fields))
        for field, check in p1.PAGE.fields.items():
            if field != 'events':
                check(value[field])
        require(key.id == '/'.join(value[k] for k in ('day', 'boot', 'first_seq')))
        events = value['events']
        require(type(events) is list and 0 < len(events) <= p1.PAGE_EVENTS)
        require(int(value['last_seq']) - int(value['first_seq']) + 1 == len(events))
    except ValueError:
        return False
    invalid = False
    for index, event in enumerate(events):
        try:
            p1.EVENT(event)
            require(int(event['seq']) == int(value['first_seq']) + index)
            wire = a2.E(value['day'], value['boot'], value['first_seq'], event['seq'], event=event)
            if event['receipt_locator'] is not None:
                require(event['receipt_locator'] == dict(day=value['day'], boot=value['boot'],
                    first_seq=value['first_seq'], event_seq=event['seq']))
            ctx.salvaged_events.append((value, event))
        except ValueError:
            invalid = True
            wire = a2.I(value['day'], value['boot'], value['first_seq'], index)
            sub = Subject(value['day'], value['boot'], wire=wire)
            ctx.issue('evidence_schema_gap', 'invalid receipt position', sub)
    return invalid
