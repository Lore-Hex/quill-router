"""One original-byte decoding and identity boundary for all gate rules."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router import async_settle_shadow_v2_a1_reconciliation as reconciliation
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Key, Ports
from trusted_router.async_settle_shadow_v2_gate_result import Subject
from trusted_router.async_settle_shadow_v2_schema import decode, require, sha256

ATTACHMENTS = {
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
    verified: set[str] = field(default_factory=set)
    obligations: list[dict[str, Any]] = field(default_factory=list)

    def kind(self, kind: str) -> list[dict[str, Any]]:
        return [v for k, v in self.rows.items() if k.kind == kind]

    def attached(self, kind: str) -> list[dict[str, Any]]:
        return [v for k, v in self.attachments.items() if k.kind == kind]

    def events(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return [(page, event) for page in self.kind(p1.KINDS[2]) for event in page['events']]

    def issue(self, gap: str, detail: str, subject: Subject = Subject(),
              ids: tuple[str, ...] = (), *, count: int | None = None) -> None:
        self.issues.append(Issue(gap, subject, detail, tuple(sorted(ids)), len(ids) or 1 if count is None else count))


def subject(value: dict[str, Any], object_id: str | None = None) -> Subject:
    return Subject(day=value.get('day'), boot=value.get('boot'), object_id=object_id)


def decode_bundle(bundle: Bundle, ports: Ports | None, gate: str) -> Context:
    ctx = Context(bundle, ports, gate)
    all_rows = (*bundle.rows, *bundle.attachments)
    seen: dict[Key, bytes] = {}
    for row in all_rows:
        sub = Subject(object_id=row.key.id)
        if row.key in seen:
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
            ctx.issue('evidence_schema_gap', str(exc), sub)
            owner = {p1.KINDS[2]: 'terminal_coverage_gap', p1.KINDS[3]: 'phase_coverage_gap',
                     p1.KINDS[4]: 'writer_closure_gap', a1.KINDS[-1]: 'close_result_identity_gap',
                     'health_basis': 'shadow_health_gap', 'publisher_trace': 'publisher_trace_gap',
                     'authorization_obligation': 'authorization_obligation_gap',
                     'handoff_accounting': 'handoff_coverage_gap'}.get(row.key.kind)
            if owner:
                ctx.issue(owner, 'invalid required row: ' + str(exc), sub)
            if str(exc) == 'a1-publisher-conservation':
                ctx.issue('publisher_accounting_gap', str(exc), sub)
    if not bundle.rows and gate != 'fleet-budgets-pre':
        ctx.issue('evidence_schema_gap', 'no evidence rows')
    try:
        require(bundle.proof is not None, 'artifact_gap')
        assert bundle.proof is not None
        ctx.proof = a1.PROOF_MANIFEST.parse(bundle.proof)
    except ValueError as exc:
        ctx.issue('artifact_gap', 'proof manifest: ' + str(exc))
    for raw in bundle.ledgers:
        try:
            ctx.ledgers.append(a1.PREREQUISITE_LEDGER.parse(raw))
        except ValueError as exc:
            ctx.issue('boot_prerequisite_gap', str(exc))
    for name in ('transport', 'fleet', 'pre', 'serving'):
        gate_raw = getattr(bundle, name)
        if gate_raw is None:
            continue
        try:
            value = a1.GATE_OUTPUT.parse(gate_raw)
            expected = {'fleet': 'fleet-budgets', 'pre': 'fleet-budgets-pre', 'serving': 'shadow-serving'}.get(name, name)
            require(value['gate'] == expected, 'artifact_gap')
            ctx.gates[name] = value
        except ValueError as exc:
            ctx.issue('artifact_gap', name + ': ' + str(exc))
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
    # Undefined constructions enter only as original bytes supplied with typed
    # inputs. There is no fallback from a digest-shaped string to a preimage.
    preimages = {p.name: p.raw for p in bundle.preimages}
    if len(preimages) != len(bundle.preimages):
        ctx.issue('artifact_gap', 'duplicate supplied preimage')
    if ctx.proof is not None:
        for name, expected in ctx.proof.items():
            if name.endswith(('_sha256', '_root')):
                original = preimages.get('proof.' + name)
                if original is None or hashlib.sha256(original).hexdigest() != expected:
                    ctx.issue('artifact_gap', 'missing or changed preimage', Subject(object_id='proof.' + name))
        for manifest in ctx.kind(p1.KINDS[5]):
            if 'router_writers' in manifest and manifest['proof_manifest_sha256'] != sha256(ctx.proof):
                ctx.issue('artifact_gap', 'manifest proof binding differs', subject(manifest))
    return ctx


def coverage_issues(ctx: Context) -> None:
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
            manifest = next((m for m in ctx.kind(p1.KINDS[5]) if m.get('day') == day and 'router_writers' in m), None)
            if manifest is not None and stream + '_root' in manifest:
                require(root == manifest[stream + '_root'], 'artifact_gap')
            for page in pages:
                for entry in page['entries']:
                    require(entry['state'] == 'present', 'artifact_gap')
                    raw = next((r.raw for r in (*ctx.bundle.rows, *ctx.bundle.attachments)
                                if r.key.id == entry['key'] and r.key.kind == (a1.KINDS[-1] if stream == 'close_result' else stream)), None)
                    require(raw is not None and hashlib.sha256(raw).hexdigest() == entry['sha256']
                            and len(raw) == entry['bytes'], 'artifact_gap')
        except ValueError as exc:
            ctx.issue('artifact_gap', str(exc), Subject(day=day, object_id=stream))


def verify_samples(ctx: Context) -> None:
    if ctx.ports is None:
        ctx.issue('artifact_gap', 'missing mandatory verifier port')
        return
    signatures = {s.object_id: s for s in ctx.bundle.signatures}
    for sample in ctx.kind(p1.KINDS[0]):
        key = sample['authorization_day'] + '/' + sample['authorization_id']
        sig = signatures.get(key)
        if sig is None:
            ctx.issue('artifact_gap', 'missing binding material', Subject(object_id=key))
            continue
        try:
            claims = ctx.ports.verify_binding(sig.token, sig.keys, sig.now)
            claims_dict = claims.model_dump(mode='json')
            require(claims_dict['authorization_id'] == sample['authorization_id'], 'identity')
            require(claims_dict['snapshot_hash'] == sample['snapshot_hash'], 'hash')
            require(sig.now == sample['observed_at_us'] // 1000000, 'proof_expired')
            reference = sample['original_terminal_receipt']
            originals = [event for page, event in ctx.events() if
                (page['day'], page['boot'], event['seq']) == (reference['day'], reference['boot'], reference['event_seq'])]
            require(len(originals) == 1 and originals[0]['identity'] == p1.committed_identity(claims_dict), 'identity')
            ctx.verified.add(key)
        except ValueError as exc:
            ctx.issue('eligible_coverage_gap', str(exc), Subject(object_id=key))
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
            result = ctx.ports.verify_certificate(sig.token, sig.keys, sig.expected, event['receipt_at_us'] // 1000000,
                returned_key=(original_key.kind, original_key.id), page=original,
                attempt=sig.attempt, server_attempt=sig.server_attempt, booking=event['booking']['outcome'])
            require(backing.partition == result.partition == 'B', result.reason or backing.reason or 'exclusion_proof_gap')
            require(sha256(sig.claims['original_receipt']) == sha256(locator), 'exclusion_proof_gap')
            ctx.verified.add(key)
        except ValueError as exc:
            ctx.issue('exclusion_conflict' if str(exc) == 'exclusion_conflict' else 'exclusion_proof_gap',
                      str(exc), subject(page, event['seq']))
