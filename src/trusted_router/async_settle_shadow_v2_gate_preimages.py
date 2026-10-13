"""A2 original-byte preimages and independently keyed F/R/C constructions."""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_result import Subject

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context


def preimages(ctx: Context, manifest: dict[str, Any]) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Derive roots from complete logical identities, retaining every candidate."""
    artifacts = {v.name: v.raw for v in ctx.bundle.preimages}
    for qualified in (*contract.OBJECT_PREIMAGES, *contract.FILE_PREIMAGES):
        dated = manifest['day'] + '/' + qualified
        if dated in artifacts:
            artifacts[qualified] = artifacts[dated]
    if ctx.bundle.proof is not None:
        artifacts['proof_manifest'] = ctx.bundle.proof
    grouped: dict[tuple[str, str], list[bytes]] = defaultdict(list)
    for row in (*ctx.bundle.rows, *ctx.bundle.attachments):
        grouped[row.key.kind, row.key.id].append(row.raw)
        artifacts['candidates/' + roots.file_digest(row.raw) + '.json'] = row.raw
    ledgers: dict[str, list[bytes]] = defaultdict(list)
    for raw in ctx.bundle.ledgers:
        try:
            ledger = a1.PREREQUISITE_LEDGER.parse(raw)
        except ValueError:
            continue
        key = a2.L(*(str(ledger[k]) for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')))
        ledgers[key].append(raw)
        artifacts['candidates/' + roots.file_digest(raw) + '.json'] = raw
    result: dict[str, Any] = {}
    day = manifest['day']
    expected = set(manifest['router_writers'])
    for field, kind in (('counter_root', p1.KINDS[3]), ('closure_root', p1.KINDS[4])):
        result['manifest.' + field] = [dict(key=a2.L(day, boot), bodies=grouped[kind, day + '/' + boot]) for boot in sorted(expected)]
    receipt_keys = {key for kind, key in grouped if kind == p1.KINDS[2] and key.startswith(day + '/')}
    result['manifest.receipts_root'] = [dict(key=a2.L(*key.split('/')), bodies=grouped[p1.KINDS[2], key]) for key in sorted(receipt_keys)]
    result['proof.ledger_root'] = [dict(key=key, bodies=values) for key, values in sorted(ledgers.items())]
    day_boots = set(manifest['router_writers']) | set(manifest['enclave_writers'])
    result['manifest.prerequisite_ledger_root'] = [entry for entry in result['proof.ledger_root'] if a2.decode_key(entry['key'])[1] in day_boots]
    trace_entries = []
    for (kind, key), values in grouped.items():
        if kind == 'publisher_trace':
            parts = key.split('/')
            if len(parts) == 4:
                trace_entries.append(dict(key=a2.L(*parts[1:]), bodies=values))
    result['proof.publisher_slot_trace_sha256'] = trace_entries
    for field, stream in contract.COVERAGE_PREIMAGES.items():
        result[field] = sorted([p for p in ctx.attached('coverage') if p['day'] == day and p['stream'] == stream], key=lambda p: p['page_seq'])
    for field, stream in contract.CROSS_DAY_PREIMAGES.items():
        result[field] = [dict(day=d, pages=sorted([p for p in ctx.attached('coverage') if p['day'] == d and p['stream'] == stream], key=lambda p: p['page_seq'])) for d in ctx.bundle.days]
    return artifacts, result


def audit(ctx: Context) -> None:
    if ctx.proof is None:
        return
    for qualified in contract.FILE_PREIMAGES:
        if not qualified.startswith('proof.'):
            continue
        expected = ctx.ports.approved_files.get(qualified) if ctx.ports else None
        if expected is None:
            ctx.issue('artifact_gap', 'approved release file pin absent', Subject(wire=a2.O('a2_file', qualified)))
        elif ctx.proof[qualified.split('.')[1]] != expected:
            ctx.issue('evidence_schema_gap', 'unapproved release fixture pin', Subject(wire=a2.O('a2_file', qualified)))
    for manifest in ctx.kind(p1.KINDS[5]):
        if 'router_writers' not in manifest:
            continue
        artifacts, root_inputs = preimages(ctx, manifest)
        try:
            contract.validate_preimages(manifest, ctx.proof, artifacts=artifacts, roots=root_inputs)
        except ValueError as exc:
            gap = str(exc) if str(exc) in ('artifact_gap', 'evidence_schema_gap') else 'evidence_schema_gap'
            ctx.issue(gap, 'A2 manifest/proof preimage validation failed',
                Subject(day=manifest['day'], wire=a2.O(p1.KINDS[5], manifest['day'] + '/manifest-v2')))


def input_inventory(ctx: Context) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Inventory exact candidate files, including conflicts, in bounded leaves."""
    from trusted_router.async_settle_shadow_v2_gate_failures import TAG
    from trusted_router.async_settle_shadow_v2_schema import sha256
    artifacts = {v.name: v.raw for v in ctx.bundle.preimages}
    for row in (*ctx.bundle.rows, *ctx.bundle.attachments):
        artifacts['rows/' + row.key.kind + '/' + row.key.id + '/' + roots.file_digest(row.raw)] = row.raw
    for raw in ctx.bundle.ledgers:
        artifacts['ledgers/' + roots.file_digest(raw)] = raw
    if ctx.bundle.proof is not None:
        artifacts['proof_manifest'] = ctx.bundle.proof
    references = [dict(path=path, sha256=roots.file_digest(raw), bytes=len(raw)) for path, raw in sorted(artifacts.items()) if len(raw) <= 262144]
    entries, leaves = [], {}
    for at in range(0, len(references), 512):
        keys = references[at:at + 512]
        raw = a2.INPUT_LEAF.serialize(dict(**TAG, keys=keys, complete=True))
        path = 'input-leaves/' + sha256(dict(index=at // 512)) + '.json'
        leaves[path] = raw
        entries.append(dict(bundle=dict(path=path, sha256=roots.file_digest(raw), bytes=len(raw)),
            first_key=keys[0]['path'], last_key=keys[-1]['path'], objects=len(keys)))
    # Leaves are output artifacts of the finite export, not members of themselves.
    roots.entry_root(entries, stream='input_inventory')
    return entries, leaves


def oversized_file_preimage(ctx: Context) -> None:
    """Closed P5 seam pending the bounded source-file reconstruction mapping.

    Keep its bytes available for F verification and every independent predicate;
    the provisional inventory cannot certify complete input closure.
    """
    for source in ctx.bundle.preimages:
        if len(source.raw) > 262144:
            ctx.issue('artifact_gap', 'oversized_file_preimage: ' + source.name,
                      Subject(wire=a2.O('a2_file', source.name)))
