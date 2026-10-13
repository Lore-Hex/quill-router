"""A2 original-byte preimages and independently keyed F/R/C constructions."""
from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_inputs import Key
from trusted_router.async_settle_shadow_v2_gate_result import Subject

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context


# These three F inputs are repository release files, not exported FILE_REFs.
# Design §7.5.4 and the A2-S8 ledger fix their paths and existing code pins.
RELEASE_FILES = {
    'proof.evaluator_fixture_sha256': 'tests/fixtures/async_settlement/billing_v1.json',
    'proof.binding_fixture_sha256': 'tests/fixtures/async_settlement/shadow_v1.json',
    'proof.wire_fixture_sha256': 'tests/fixtures/async_settlement/shadow_v2/manifest.json',
}


def preimages(ctx: Context, manifest: dict[str, Any]) -> tuple[dict[str, bytes], dict[str, Any]]:
    """Derive roots from complete logical identities, retaining every candidate."""
    artifacts = {v.name: v.raw for v in ctx.bundle.preimages if v.name not in RELEASE_FILES}
    artifacts.update(ctx.release_files)
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
    # A missing page is still a root entry. Enumerate the first committed page
    # and each successor from the counter's HWM and verified predecessor, not
    # merely from the files that happened to arrive in the export.
    receipt_keys = {key for kind, key in grouped if kind == p1.KINDS[2] and key.startswith(day + '/')}
    for counter in ctx.kind(p1.KINDS[3]):
        if counter['day'] != day or counter['durable_event_hwm'] == 0:
            continue
        first = 1
        while first <= counter['durable_event_hwm']:
            key = day + '/' + counter['boot'] + '/' + f'{first:020}'
            receipt_keys.add(key)
            page = ctx.rows.get(Key(p1.KINDS[2], key))
            if page is None:
                break
            first = int(page['last_seq']) + 1
    result['manifest.receipts_root'] = [dict(key=a2.L(*key.split('/')), bodies=grouped[p1.KINDS[2], key]) for key in sorted(receipt_keys)]
    expected_ledgers = {a2.L(b.role, b.boot, b.revision, b.region, str(b.start_us))
                        for b in ctx.bundle.inventory.boots} if ctx.bundle.inventory else set()
    result['proof.ledger_root'] = [dict(key=key, bodies=ledgers[key]) for key in sorted(expected_ledgers)]
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
    for issue in ctx.bundle.a2_streams.get('loading_issues', []):
        ctx.issue(issue['gap'], issue['detail'], Subject(wire=a2.O('a2_file', issue['path'])))
    if 'loading_issues' in ctx.bundle.a2_streams:
        ledger_preimages(ctx)
    if ctx.proof is None:
        return
    expected_ledgers = {a2.L(b.role, b.boot, b.revision, b.region, str(b.start_us))
                        for b in ctx.bundle.inventory.boots} if ctx.bundle.inventory else set()
    actual_ledgers = {a2.L(*(str(ledger[k]) for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')))
                      for ledger in ctx.ledgers}
    for key in sorted(expected_ledgers - actual_ledgers):
        ctx.issue('artifact_gap', 'expected prerequisite ledger absent', Subject(wire=a2.O('prerequisite_ledger', key)))
    for key in sorted(actual_ledgers - expected_ledgers):
        ctx.issue('evidence_schema_gap', 'prerequisite ledger outside independent inventory', Subject(wire=a2.O('prerequisite_ledger', key)))
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
            inventory = contract._object_bytes(artifacts['manifest.independent_inventory_sha256'], a2.INDEPENDENT_INVENTORY)
            if inventory['day'] != manifest['day']:
                raise ValueError('evidence_schema_gap')
            contract.validate_preimages(manifest, ctx.proof, artifacts=artifacts, roots=root_inputs)
        except KeyError:
            ctx.issue('artifact_gap', 'independent inventory preimage absent',
                Subject(day=manifest['day'], wire=a2.O(p1.KINDS[5], manifest['day'] + '/manifest-v2')))
        except ValueError as exc:
            gap = str(exc) if str(exc) in ('artifact_gap', 'evidence_schema_gap') else 'evidence_schema_gap'
            ctx.issue(gap, 'A2 manifest/proof preimage validation failed',
                Subject(day=manifest['day'], wire=a2.O(p1.KINDS[5], manifest['day'] + '/manifest-v2')))


def ledger_preimages(ctx: Context) -> None:
    """Resolve ledger F commitments from named original files, never hex alone.

    The descriptor has no path. All candidates must therefore be in the finite
    supplied inventory; no filesystem search is permitted. Unknown prerequisite
    evidence formats have an explicit parser seam, not a completion-time guess.
    """
    files: dict[str, set[bytes]] = defaultdict(set)
    for source in ctx.bundle.preimages:
        if source.name not in RELEASE_FILES and len(source.raw) <= 262144:
            files[roots.file_digest(source.raw)].add(source.raw)
    previous_files: dict[str, set[bytes]] = defaultdict(set)
    for raw in ctx.bundle.ledgers:
        previous_files[roots.file_digest(raw)].add(raw)
    for ledger in ctx.ledgers:
        key = a2.L(*(str(ledger[k]) for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')))
        subject = Subject(wire=a2.O('prerequisite_ledger', key))
        for field in ('policy_sha256', 'config_sha256', 'fixture_sha256'):
            if len(files[ledger[field]]) != 1:
                ctx.issue('artifact_gap', 'ledger file preimage absent or ambiguous: ' + field, subject)
        previous = ledger['previous_ledger_sha256']
        if previous is not None:
            candidates = previous_files[previous]
            if len(candidates) != 1:
                ctx.issue('artifact_gap', 'previous exact ledger preimage absent or ambiguous', subject)
            else:
                try:
                    contract._object_bytes(next(iter(candidates)), a1.PREREQUISITE_LEDGER)
                except ValueError:
                    ctx.issue('evidence_schema_gap', 'invalid previous ledger preimage', subject)
        for field in ('transport', 'size', 'regional_load', 'boot_transport', 'observer_freshness', 'marker_keys'):
            artifact = ledger[field]
            candidates = files[artifact['sha256']]
            if len(candidates) != 1:
                ctx.issue('artifact_gap', 'prerequisite evidence preimage absent or ambiguous: ' + field, subject)
                continue
            raw = next(iter(candidates))
            # These are the only contract-owned completed-evidence wrappers.
            # Retained/opaque tool outputs require the named retained parser.
            evidence = None
            for schema in (a1.GATE_OUTPUT, a2.PROOF_RESULT):
                try:
                    evidence = contract._object_bytes(raw, schema)
                except ValueError:
                    continue
                break
            if evidence is None:
                ctx.issue('evidence_schema_gap', 'ledger_artifact_parser_unbound:' + field, subject)
            elif evidence['completed_at_us'] != artifact['completed_at_us']:
                ctx.issue('artifact_gap', 'prerequisite completion differs from original evidence: ' + field, subject)
            elif 'criterion' in evidence:
                if evidence['criterion'] != field or evidence[ledger['role'] + '_revision'] != ledger['revision']:
                    ctx.issue('evidence_schema_gap', 'prerequisite criterion or producer revision differs: ' + field, subject)
            else:
                # A generic gate output does not establish a particular boot's
                # readiness (§5.3); don't invent a mapping to daily fleet gates.
                ctx.issue('evidence_schema_gap', 'ledger_artifact_context_unbound:' + field, subject)


def input_inventory(ctx: Context) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """Inventory exact candidate files, including conflicts, in bounded leaves."""
    from trusted_router.async_settle_shadow_v2_gate_failures import TAG
    from trusted_router.async_settle_shadow_v2_schema import sha256
    artifacts = {v.name: v.raw for v in ctx.bundle.preimages if v.name not in RELEASE_FILES}
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


def verify_release_file_preimages(ctx: Context) -> None:
    """Read fixed checkout inputs through the tool's I/O port, checking both pins.

    Exported bytes cannot substitute for a release input. The reader is bound
    to the tool checkout, never to a caller-supplied bundle path. All other
    supplied evidence remains subject to FILE_REF's original-byte bound.
    """
    for qualified, path in RELEASE_FILES.items():
        subject = Subject(wire=a2.O('a2_file', path))
        if ctx.ports is None or ctx.ports.read_release_file is None or ctx.proof is None:
            ctx.issue('artifact_gap', 'release checkout reader absent', subject)
            continue
        try:
            raw = ctx.ports.read_release_file(path)
            actual = roots.file_digest(raw)
        except (OSError, ValueError):
            ctx.issue('artifact_gap', 'release checkout file unavailable', subject)
            continue
        pin = ctx.ports.approved_files.get(qualified)
        if pin is None or actual != pin or actual != ctx.proof[qualified.split('.')[1]]:
            ctx.issue('artifact_gap', 'release checkout file differs from repository or manifest pin', subject)
            continue
        ctx.release_files[qualified] = raw
    for source in ctx.bundle.preimages:
        if source.name not in RELEASE_FILES and len(source.raw) > 262144:
            ctx.issue('artifact_gap', 'exported_file_ref_bound: ' + source.name,
                      Subject(wire=a2.O('a2_file', source.name)))
