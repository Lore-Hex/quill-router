"""Round 2 changes to original fixture files and their declared references."""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

from tests.shadow_v2_gate_a2_fixture import commit_originals, transport
from tests.test_async_settle_shadow_v2_gate_loading import detail, reference
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
from trusted_router.async_settle_shadow_v2_gate_inputs import SHUTDOWN_SOURCE, Row
from trusted_router.detached_jws import canonical


def shutdown_originals(original: Any, ports: Any, per_writer: dict[str, dict[str, int]] | None = None, **changes: int) -> tuple[bytes, Any]:
    files = {p.name: p.raw for p in original.preimages}
    attachments = []
    measurements = {}
    for row in original.attachments:
        if row.key.kind != 'shutdown_measurement':
            continue
        value = json.loads(row.raw)
        value.update(changes if per_writer is None else per_writer[value['boot']])
        source = {k: v for k, v in value.items() if k != 'source'}
        value['source'] = reference(files, value['source']['path'], SHUTDOWN_SOURCE.serialize(source))
        measurements[value['day'], value['boot']] = reference(files, row.key.id, a2.SHUTDOWN_MEASUREMENT.serialize(value))
    entries = []
    for row in original.attachments:
        if row.key.kind == 'shutdown_measurement':
            row = Row(row.key, files[row.key.id])
        elif row.key.kind == 'shutdown_entry':
            value = json.loads(row.raw)
            value['measurement'] = measurements[value['day'], value['boot']]
            value['grace_us'] = (changes if per_writer is None else per_writer[value['boot']]).get('grace_us', value['grace_us'])
            entries.append(value)
            row = Row(row.key, a2.SHUTDOWN_ENTRY.serialize(value))
        attachments.append(row)
    envelope = json.loads(files['proof.shutdown_inventory_sha256'])
    envelope['writers'] = detail(files, entries, 'shutdown_writers')
    envelope['measurements'] = reference(files, envelope['measurements']['path'], canonical([e['measurement'] for e in entries]))
    review = json.loads(files[envelope['approval']['record']['path']])
    review['subject_sha256'] = envelope['measurements']['sha256']
    envelope['approval']['subject_sha256'] = review['subject_sha256']
    envelope['approval']['record'] = reference(files, envelope['approval']['record']['path'], canonical(review))
    files['proof.shutdown_inventory_sha256'] = canonical(envelope)
    plan = json.loads(ports.release.raw)
    plan['artifacts'].update({path: file_digest(raw) for path, raw in files.items()
                              if path != ports.release.path and not path.startswith('proof.')})
    files[ports.release.path] = canonical(plan)
    ports = replace(ports, release=replace(ports.release, raw=files[ports.release.path], artifacts=plan['artifacts']))
    return transport(commit_originals(replace(original, attachments=tuple(attachments)), files)), ports


def compact_originals(original: Any, ports: Any, stage: str) -> Any:
    """Keep the seven-day evidence and descriptors; drop replaced source copies."""
    from trusted_router import async_settle_shadow_v2_a2_contract as contract
    from trusted_router.async_settle_shadow_v2_gate_dependencies import dependencies
    from trusted_router.async_settle_shadow_v2_gate_inputs import Preimage
    files = {p.name: p.raw for p in original.preimages}
    payloads = {path: contract._object_bytes(raw, contract.OBJECT_PREIMAGES[qualified])
                for path, raw in files.items()
                if (qualified := path.split('/', 1)[-1] if '/manifest.' in path else path) in contract.OBJECT_PREIMAGES}
    required = set(dependencies(files, payloads, ports, stage, original.ledgers))
    # The unchanged preimage validator checks every FILE_REF directly present
    # in the supplied root objects, including deferred proof descriptors.
    pending = list(payloads.values())
    while pending:
        node = pending.pop()
        if isinstance(node, dict):
            if set(node) == set(a2.FILE_REF.fields):
                required.add(node['path'])
            else:
                pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
    required.update(path for path in files if path.startswith(('proof.', 'prior-', 'loading/')) or '/manifest.' in path)
    return replace(original, preimages=tuple(Preimage(path, raw) for path, raw in files.items() if path in required))


def opaque_originals(original: Any, ports: Any, body: bytes) -> tuple[bytes, Any]:
    """Register a support file with no declared JSON format."""
    from trusted_router.async_settle_shadow_v2_schema import sha256
    files = {p.name: p.raw for p in original.preimages}
    external = json.loads(files['proof.external_proofs_sha256'])
    required = next(row for row in external['requirements'] if row['stage'] == 'transport')
    proof = json.loads(files[required['evidence']['path']])
    tail = proof['sources']['tail_sha256']
    entries: list[dict[str, Any]] = []
    while tail is not None:
        page = next(json.loads(raw) for raw in files.values() if raw.startswith(b'{')
                    and 'entries' in json.loads(raw) and sha256(json.loads(raw)) == tail)
        entries[:0] = page['entries']
        tail = page['previous_sha256']
    entries.append(reference(files, 'ci/review/workflow.yml', body))
    proof['sources'] = detail(files, sorted(entries, key=lambda row: row['path']), 'files')
    required['evidence'] = reference(files, required['evidence']['path'], canonical(proof))
    review = json.loads(files[required['approval']['record']['path']])
    review['subject_sha256'] = required['evidence']['sha256']
    required['approval']['subject_sha256'] = required['evidence']['sha256']
    required['approval']['record'] = reference(files, required['approval']['record']['path'], canonical(review))
    files['proof.external_proofs_sha256'] = canonical(external)
    plan = json.loads(ports.release.raw)
    plan['artifacts'].update({path: file_digest(raw) for path, raw in files.items()
        if path != ports.release.path and not path.startswith('proof.')})
    files[ports.release.path] = canonical(plan)
    ports = replace(ports, release=replace(ports.release, raw=files[ports.release.path], artifacts=plan['artifacts']))
    return transport(commit_originals(original, files)), ports


def captured_plan(raw: bytes, root: Any, *, part_size: int = 200) -> Any:
    """Write the C2 captured-transport and pack-plan formats from originals."""
    from trusted_router.async_settle_shadow_v2_gate_plan import PlanInput
    value = json.loads(raw)
    files: dict[str, bytes] = {}
    metadata = {k: v for k, v in value.items() if k not in ('rows', 'attachments', 'preimages', 'ledgers')}
    meta = reference(files, 'transport/metadata.json', canonical(metadata))
    parts = []
    children = [dict(member=group, value=child) for group in ('rows', 'attachments', 'preimages', 'ledgers') for child in value.get(group, [])]
    batch = []
    for index, child in enumerate(children):
        encoded = canonical(child)
        if len(encoded) > 200000:
            child = dict(member=child['member'], encoded=[reference(files, f'transport/pieces/{index}/{offset}', encoded[offset:offset + 200000])
                for offset in range(0, len(encoded), 200000)])
        if batch and (len(batch) >= part_size or len(canonical([*batch, child])) > 262144):
            parts.append(reference(files, f'transport/parts/{len(parts):05}.json', canonical(batch)))
            batch = []
        batch.append(child)
    if batch:
        parts.append(reference(files, f'transport/parts/{len(parts):05}.json', canonical(batch)))
    refs = [dict(path=path, bytes=len(body), sha256=file_digest(body)) for path, body in sorted(files.items())]
    entries = []
    for index in range(0, len(refs), 512):
        keys = refs[index:index + 512]
        leaf = reference(files, f'inputs/leaves/{index:05}.json', a2.INPUT_LEAF.serialize(dict(
            v=2, policy_version='shadow-v2', contract_revision='a2', keys=keys, complete=True)))
        entries.append(dict(bundle=leaf, first_key=keys[0]['path'], last_key=keys[-1]['path'], objects=len(keys)))
    before = set(files)
    inventory = detail(files, entries, 'input_inventory')
    pages = [dict(path=path, bytes=len(files[path]), sha256=file_digest(files[path])) for path in files.keys() - before]
    inputs = dict(format='shadow-v2-pack-plan-1', days=value['days'], predecessors=[],
        inventory=inventory, bundles=entries, objects=len(refs), inventory_pages=pages)
    plan = dict(format='shadow-v2-captured-transport-1', metadata=meta, parts=parts, objects=len(children), inputs=inputs)
    root.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    plan_raw = canonical(plan)
    (root / 'transport-plan.json').write_bytes(plan_raw)
    return PlanInput(plan_raw, lambda name: (root / name).read_bytes())


def accounted_originals(original: Any, ports: Any, *, damage: str = '') -> tuple[bytes, Any]:
    """Build complete original pass pages, then bind them to the deployment."""
    from trusted_router.async_settle_shadow_v2_gate_native_accounting import (
        DAY_KINDS,
        OBJECT_KINDS,
        bounds,
    )
    files = {p.name: p.raw for p in original.preimages}
    for date in original.days:
        low, high = bounds(date)
        entries, reads, planned = [], [], []
        selected = [row for row in (*original.rows, *original.attachments)
            if row.key.kind in DAY_KINDS and row.key.id.startswith(low) and not row.key.id.endswith('/manifest-v2')
            or row.key.kind in OBJECT_KINDS.values() and '/' + date + '/' in row.key.id]
        for kind in DAY_KINDS:
            planned.append(dict(source='rows', kind=kind, day=date, start=low, end=high))
            own = sorted((row for row in selected if row.key.kind == kind), key=lambda row: row.key.id)
            assert len(own) < 200
            reads.append(dict(source='rows', kind=kind, snapshot=1, after='', start=low, end=high,
                count=len(own), first=own[0].key.id if own else None, last=own[-1].key.id if own else None,
                page_size=200, complete=True, continuation=None, status='ok', retries=0, priority='LOW',
                deadline_ms=200, body_bytes=sum(len(row.raw) for row in own),
                key_bytes=sum(len(kind.encode()) + len(row.key.id.encode()) for row in own), native_rpcs=1, elapsed_us=1))
            for row in own:
                ref = reference(files, f'accounting/{date}/originals/{len(entries)}', row.raw)
                entries.append(dict(ordinal=len(entries), kind=kind, key=row.key.id, original=ref['path'],
                    sha256=ref['sha256'], bytes=ref['bytes'], key_bytes=len(kind.encode()) + len(row.key.id.encode()),
                    key_sha256=file_digest(canonical([kind, row.key.id])), key_sha256_encoding='canonical-json-kind-key',
                    metadata=dict(read_snapshot_ns=1, kind=kind)))
        for row in selected:
            if row.key.kind in DAY_KINDS:
                continue
            planned.append(dict(source='object', key=row.key.id, generation='1'))
            reads.append(dict(source='object', key=row.key.id, generation='1', metageneration='1',
                generation_pinned=True, status='ok', complete=True, count=1, retries=0, deadline_ms=200,
                body_bytes=len(row.raw), native_rpcs=1, elapsed_us=1))
            ref = reference(files, f'accounting/{date}/originals/{len(entries)}', row.raw)
            entries.append(dict(ordinal=len(entries), kind=row.key.kind, key=row.key.id, original=ref['path'],
                sha256=ref['sha256'], bytes=ref['bytes'], key_bytes=len(row.key.kind.encode()) + len(row.key.id.encode()),
                key_sha256=file_digest(canonical([row.key.kind, row.key.id])), key_sha256_encoding='canonical-json-kind-key',
                metadata=dict(generation='1', metageneration='1')))
        if damage == 'cursor':
            reads[0]['after'] = 'changed'
        if damage == 'extra-rpc':
            reads[0]['native_rpcs'] += 1000000000
        if damage == 'unaccounted':
            entries.pop()
        collector_plan = {}
        if damage in ('collector-object', 'planned-collector'):
            entry = next(e for e in entries if e['kind'] in OBJECT_KINDS.values())
            entry['metadata']['source'] = 'supplied_collector_original'
            if damage == 'planned-collector':
                collector_plan[entry['key']] = {k: entry[k] for k in ('kind', 'sha256', 'bytes')}
                planned = [p for p in planned if p.get('key') != entry['key']]
                reads = [p for p in reads if p.get('key') != entry['key']]
        if damage == 'collector-row':
            kind = next(e['kind'] for e in entries if e['kind'] in DAY_KINDS)
            for entry in entries:
                if entry['kind'] == kind:
                    entry['metadata']['source'] = 'supplied_collector_original'
            for page in reads:
                if page.get('kind') == kind:
                    page.update(count=0, first=None, last=None, body_bytes=0, key_bytes=0)
        details = {}
        for stream, values in [('plan', planned), ('entries', entries), ('reads', reads)]:
            details[stream] = [reference(files, f'accounting/{date}/{stream}.json', canonical(dict(
                stream=stream, sequence=0, previous_sha256=None, entries=values)))]
        root = dict(format='shadow-v2-live-export-a3-n', native=True, complete=True, day=date, passes=1,
            source_body_bytes=sum(e['bytes'] for e in entries), source_key_bytes=sum(e['key_bytes'] for e in entries),
            source_operations=len(reads), gap_count=0, entry_count=len(entries), details=details,
            operations=dict(native_rpcs=sum(p['native_rpcs'] for p in reads), pages=sum(p['source'] == 'rows' for p in reads),
                rows=sum(p['count'] for p in reads if p['source'] == 'rows'), objects=sum(p['source'] == 'object' for p in reads), body_bytes=sum(p['body_bytes'] for p in reads),
                elapsed_us=sum(p['elapsed_us'] for p in reads)))
        ref = reference(files, f'accounting/{date}/root.json', canonical(root))
        path = date + '/manifest.independent_inventory_sha256'
        envelope = json.loads(files[path])
        deployment = json.loads(files[envelope['deployment']['path']])
        deployment['export_accounting'] = detail(files, [ref], 'files')
        if collector_plan:
            deployment['collector_plan'] = collector_plan
        envelope['deployment'] = reference(files, envelope['deployment']['path'], canonical(deployment))
        review = json.loads(files[envelope['approval']['record']['path']])
        review['subject_sha256'] = envelope['deployment']['sha256']
        envelope['approval']['subject_sha256'] = review['subject_sha256']
        envelope['approval']['record'] = reference(files, envelope['approval']['record']['path'], canonical(review))
        files[path] = canonical(envelope)
        scope = json.loads(files['loading/qualification-scope.json'])
        scope['trial_inventories'] = [reference(files, ref['path'], files[ref['path']]) for ref in scope['trial_inventories']]
        files['loading/qualification-scope.json'] = canonical(scope)
        if damage == 'missing':
            del files[ref['path']]
    plan = json.loads(ports.release.raw)
    plan['artifacts'].update({path: file_digest(raw) for path, raw in files.items()
        if path != ports.release.path and not path.startswith('proof.')})
    files[ports.release.path] = canonical(plan)
    ports = replace(ports, release=replace(ports.release, raw=files[ports.release.path], artifacts=plan['artifacts']))
    raw = transport(commit_originals(original, files))
    if damage in ('root-shape', 'page-shape'):
        target = '/root.json' if damage == 'root-shape' else '/reads.json'
        value = json.loads(raw)
        next(p for p in value['preimages'] if p['name'].endswith(target))['raw'] = '{}'
        raw = canonical(value)
    return raw, ports
