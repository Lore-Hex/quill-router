"""Original export accounting and bottom-up day file assembly."""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as contract
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_export import DAY_KINDS, OBJECT_KINDS, bounds
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import require, sha256
from trusted_router.detached_jws import canonical


@dataclass(frozen=True)
class AccountingResult:
    day: str
    sources: tuple[dict[str, Any], ...]
    missing: tuple[str, ...]
    native_rpcs: int
    body_bytes: int
    operations: int

    @property
    def complete(self) -> bool:
        return not self.missing


def compare_export_accounting(files: Files, reference: dict[str, Any], bundle: bytes, *,
                              day: str, object_plan: Mapping[str, str | None],
                              prefix: str = '', require_native: bool = True,
                              collector_plan: Mapping[str, dict[str, Any]] | None = None) -> AccountingResult:
    """Reference parser for G1/G7; plans precede captured presence lookup.

    Paths inside each exporter pass are relative to that pass's original spool.
    Prefixing a FILE_REF here locates bytes; it never rewrites captured objects.
    Unsuccessful pages and all received candidates remain inventory inputs.
    """
    collector_plan = collector_plan or {}
    low, high = bounds(day)
    expected_ranges = set(DAY_KINDS)
    missing: list[str] = []
    sources = [reference]

    def need(condition: bool, reason: str) -> None:
        if not condition and reason not in missing:
            missing.append(reason)

    def read(ref: dict[str, Any]) -> bytes:
        located = {**ref, 'path': prefix + ref['path']}
        sources.append(located)
        return files.read(located)

    root = retained_json(files.read(reference))
    require(root['format'] == 'shadow-v2-live-export-a3-n', 'evidence_schema_gap')
    require(type(root['native']) is bool and type(root['complete']) is bool, 'evidence_schema_gap')
    for field in ('passes', 'source_body_bytes', 'source_key_bytes', 'source_operations', 'gap_count', 'entry_count'):
        require(type(root[field]) is int and root[field] >= 0, 'evidence_schema_gap')
    require(set(root['details']) <= {'plan', 'entries', 'reads', 'gaps', 'final-gaps'}, 'evidence_schema_gap')
    require(all(type(value) is int and value >= 0 for value in root['operations'].values()), 'evidence_schema_gap')
    need(not require_native or root['native'], 'native_provenance')
    need(root['day'] == day and root['passes'] == 1, 'pass_scope')
    streams: dict[str, list[dict[str, Any]]] = {}
    for stream in ('plan', 'entries', 'reads', 'gaps', 'final-gaps'):
        refs = root['details'].get(stream, [])
        previous = None
        entries = []
        for index, ref in enumerate(refs):
            raw = read(ref)
            page = retained_json(raw)
            need(page['stream'] == stream and page['sequence'] == index and
                 page['previous_sha256'] == previous, 'accounting_chain:' + stream)
            need(0 < len(page['entries']) <= 128, 'accounting_page_size:' + stream)
            if index < len(refs) - 1:
                need(len(page['entries']) == 128, 'accounting_short_middle:' + stream)
            previous = roots.file_digest(raw)
            entries.extend(page['entries'])
        streams[stream] = entries
    need(all(p.get('source') in ('rows', 'object') for p in streams['plan']), 'unplanned_source')
    planned_ranges = [p for p in streams['plan'] if p.get('source') == 'rows']
    planned_objects = [p for p in streams['plan'] if p.get('source') == 'object']
    need(Counter(p['kind'] for p in planned_ranges) == Counter(expected_ranges), 'planned_ranges')
    need(all(p['day'] == day and p['start'] == low and p['end'] == high for p in planned_ranges), 'planned_days')
    need(Counter(p['key'] for p in planned_objects) == Counter(list(object_plan)), 'planned_objects')
    for p in planned_objects:
        need(p['key'] in object_plan and (object_plan[p['key']] is None or
             p['generation'] == object_plan[p['key']]), 'planned_generation:' + p['key'])
    value = retained_json(bundle, limit=10000 * 262144)
    transport = Counter((row['kind'], row['id'], row['raw'].encode())
                        for group in ('rows', 'attachments') for row in value.get(group, []))
    native_keys = {(r['kind'], r['id']) for r in value.get('rows', [])}
    originals: Counter[tuple[str, str, bytes]] = Counter()
    entries = streams['entries']
    for ordinal, entry in enumerate(entries):
        ref = dict(path=entry['original'], sha256=entry['sha256'], bytes=entry['bytes'])
        raw = read(ref)
        need(entry['ordinal'] == ordinal, 'entry_ordinal')
        need(entry['key_bytes'] == len(entry['kind'].encode()) + len(entry['key'].encode()), 'key_bytes')
        need(entry['key_sha256'] == roots.file_digest(canonical([entry['kind'], entry['key']])), 'key_digest')
        need(entry['key_sha256_encoding'] == 'canonical-json-kind-key', 'key_encoding')
        originals[entry['kind'], entry['key'], raw] += 1
    need(originals == transport, 'bundle_originals')
    need(all(n == 1 for n in Counter((e['kind'], e['key']) for e in entries).values()), 'duplicate_candidates')
    need(all(e['metadata'].get('source') != 'supplied_collector_original'
             for e in entries if (e['kind'], e['key']) in native_keys), 'native_member_label')
    collectors = [e for e in entries if e['metadata'].get('source') == 'supplied_collector_original']
    need({e['key'] for e in collectors} == set(collector_plan), 'planned_collector_attachments')
    for entry in collectors:
        need(entry['kind'] not in expected_ranges and (entry['kind'], entry['key']) not in native_keys and
            collector_plan.get(entry['key']) == dict(kind=entry['kind'], sha256=entry['sha256'], bytes=entry['bytes']),
            'collector_original')
    reads = streams['reads']
    snapshots = {p.get('snapshot') for p in reads if p.get('source') == 'rows'}
    need(len(snapshots) == 1 and None not in snapshots and 0 not in snapshots, 'pass_snapshot')
    need(all(p.get('source') in ('rows', 'object') for p in reads), 'unplanned_read')
    for kind in sorted(expected_ranges):
        pages = [p for p in reads if p.get('source') == 'rows' and p.get('kind') == kind]
        rows = [e for e in entries if e['kind'] == kind and (e['kind'], e['key']) in native_keys]
        cursor = ''
        offset = 0
        need(bool(pages), 'range_missing:' + kind)
        for index, page in enumerate(pages):
            count = page['count']
            require(type(count) is int and count >= 0, 'evidence_schema_gap')
            current = rows[offset:offset + count]
            offset += count
            keys = [e['key'] for e in current]
            need(page['after'] == cursor and all(low <= key < high and key > cursor for key in keys)
                 and keys == sorted(set(keys)), 'range_cursor:' + kind)
            need(page['start'] == low and page['end'] == high, 'range_bounds:' + kind)
            need(page['count'] == len(current) and page['first'] == (keys[0] if keys else None)
                 and page['last'] == (keys[-1] if keys else None), 'range_records:' + kind)
            need(0 <= count <= page['page_size'] <= 200 and page['page_size'] > 0, 'range_page_size:' + kind)
            need(type(page['complete']) is bool and page['complete'] == (index == len(pages) - 1)
                 and page['complete'] == (count < page['page_size']) and
                 page['continuation'] == (None if page['complete'] else page['last']), 'range_eof:' + kind)
            need(page['status'] == 'ok' and page['retries'] == 0 and page['priority'] == 'LOW'
                 and 0 < page['deadline_ms'] <= 200, 'range_call:' + kind)
            need(page.get('body_bytes') == sum(e['bytes'] for e in current) and
                 page.get('key_bytes') == sum(e['key_bytes'] for e in current), 'range_bytes:' + kind)
            if root['native']:
                need(all(e['metadata'].get('read_snapshot_ns') == page['snapshot'] and
                         e['metadata'].get('kind') == kind and not e['metadata'].get('received_before_failure')
                         for e in current), 'row_metadata:' + kind)
            cursor = keys[-1] if keys else cursor
        need(offset == len(rows), 'range_unconsumed:' + kind)
    need({p['kind'] for p in reads if p.get('source') == 'rows'} == expected_ranges, 'read_ranges')
    object_reads = [p for p in reads if p.get('source') == 'object']
    need(Counter(p['key'] for p in object_reads) == Counter(list(object_plan)), 'object_calls')
    for key, generation in object_plan.items():
        selected = [e for e in entries if e['key'] == key and e['kind'] == OBJECT_KINDS.get(key.split('/')[0])]
        pages = [p for p in object_reads if p['key'] == key]
        need(len(selected) == len(pages) == 1, 'object_once:' + key)
        if len(selected) != 1 or len(pages) != 1:
            continue
        entry, page = selected[0], pages[0]
        need(page['status'] == 'ok' and page['complete'] is True and page['count'] == 1 and
             page['retries'] == 0 and 0 < page['deadline_ms'] <= 200, 'object_call:' + key)
        need(bool(entry['metadata'].get('generation')) and
             (generation is None or generation == entry['metadata']['generation']), 'object_generation:' + key)
        if root['native']:
            need(page.get('generation') == entry['metadata'].get('generation') and
                 page.get('metageneration') == entry['metadata'].get('metageneration') and
                 page.get('generation_pinned') is True, 'object_metadata:' + key)
        need(page.get('body_bytes') == entry['bytes'], 'object_bytes:' + key)
    for page in reads:
        need(type(page.get('native_rpcs')) is int and page['native_rpcs'] >= 0, 'rpc_measurement')
        need(type(page.get('elapsed_us')) is int and 0 <= page['elapsed_us'] <= page['deadline_ms'] * 1000,
             'call_elapsed')
    actual = dict(native_rpcs=sum(p.get('native_rpcs', 0) for p in reads),
        pages=sum(p['source'] == 'rows' for p in reads),
        rows=sum(p['count'] for p in reads if p['source'] == 'rows'),
        objects=sum(p['count'] for p in reads if p['source'] == 'object'),
        body_bytes=sum(p.get('body_bytes', 0) for p in reads),
        elapsed_us=sum(p.get('elapsed_us', 0) for p in reads))
    need(root['operations'] == actual, 'operation_totals')
    native_entries = [e for e in entries if e not in collectors]
    need(actual['body_bytes'] == sum(e['bytes'] for e in native_entries), 'native_body_totals')
    need(sum(p.get('key_bytes', 0) for p in reads if p['source'] == 'rows') ==
         sum(e['key_bytes'] for e in native_entries if (e['kind'], e['key']) in native_keys),
         'native_key_totals')
    need(root['entry_count'] == len(entries) and root['source_operations'] == len(reads)
         and root['source_body_bytes'] == sum(e['bytes'] for e in entries)
         and root['source_key_bytes'] == sum(e['key_bytes'] for e in entries), 'source_totals')
    need(root['gap_count'] == len(streams['gaps']) + len(streams['final-gaps']), 'failure_totals')
    need(root['complete'] is True and root['gap_count'] == 0, 'export_incomplete')
    unique = {ref['path']: ref for ref in sources}
    return AccountingResult(day, tuple(unique[k] for k in sorted(unique)), tuple(missing),
                            actual['native_rpcs'], root['source_body_bytes'], len(reads))


def bind_accounting(files: PackFiles, deployment: dict[str, Any], *,
                    day: str, accounting: list[dict[str, Any]]) -> dict[str, Any]:
    require(bool(accounting), 'artifact_gap')
    for ref in accounting:
        value = retained_json(files.read(ref))
        require(value['format'] == 'shadow-v2-live-export-a3-n' and value['day'] == day, 'artifact_gap')
    require('export_accounting' not in deployment, 'artifact_gap')
    return {**deployment, 'export_accounting': files.detail(day + '/accounting', accounting, 'files')}


def select_manifests(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [a1.validate_row(row['kind'], row['id'], row['raw'].encode()) for row in rows
            if row['kind'] == a1.KINDS[5] and row['id'].endswith('/manifest-v2')]


def assemble_day(files: PackFiles, manifest_fields: dict[str, Any], proof_fields: dict[str, Any], *,
                 artifacts: Mapping[str, bytes], preimages: dict[str, Any],
                 expected_closed: Mapping[str, bool], horizon_ends: list[int],
                 complete_through_us: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """The proof precedes the day manifest; reports are downstream outputs."""
    require(bool(expected_closed), 'artifact_gap')
    complete = all(value is True for value in expected_closed.values()) and all(
        horizon <= complete_through_us for horizon in horizon_ends)
    manifest, proof = dict(manifest_fields), dict(proof_fields)
    require(not manifest.get('complete') or complete, 'artifact_gap')
    require(not proof.get('complete') or complete, 'artifact_gap')
    supplied = dict(artifacts)
    for qualified in (*contract.FILE_PREIMAGES, *contract.OBJECT_PREIMAGES):
        raw = supplied[qualified]
        owner, field = qualified.split('.')
        value = roots.file_digest(raw)
        if qualified in contract.OBJECT_PREIMAGES:
            decoded = contract._object_bytes(raw, contract.OBJECT_PREIMAGES[qualified])
            if 'complete' in decoded:
                complete = complete and decoded['complete'] is True
            value = sha256(decoded)
        (manifest if owner == 'manifest' else proof)[field] = value
    for qualified, stream in contract.ENTRY_PREIMAGES.items():
        entries = [roots._root_entry(c['key'], c['bodies']) for c in preimages[qualified]]
        for candidate in preimages[qualified]:
            for body in candidate['bodies']:
                path = 'candidates/' + roots.file_digest(body)
                files.put(path, body)
                supplied[path] = body
        detail = files.detail(manifest['day'] + '/roots/' + qualified, entries, stream)
        files.obj(manifest['day'] + '/roots/' + qualified + '.json', detail)
        complete = complete and all(entry['state'] == 'present' for entry in entries)
        owner, field = qualified.split('.')
        (manifest if owner == 'manifest' else proof)[field] = roots.entry_root(entries, stream=stream)
    for qualified in contract.COVERAGE_PREIMAGES:
        manifest[qualified.split('.')[1]] = a1.coverage_root(preimages[qualified])
        complete = complete and all(entry['state'] == 'present' for page in preimages[qualified] for entry in page['entries'])
    for qualified, stream in contract.CROSS_DAY_PREIMAGES.items():
        entries = [dict(day=d['day'], root=a1.coverage_root(d['pages']),
                        count=sum(len(p['entries']) for p in d['pages'])) for d in preimages[qualified]]
        proof[qualified.split('.')[1]] = roots.entry_root(entries, stream=stream + '_root')
    require(not manifest.get('complete') or complete, 'artifact_gap')
    require(not proof.get('complete') or complete, 'artifact_gap')
    manifest['complete'] = proof['complete'] = complete
    # Only upstream supplied originals may be committed as proof attachments.
    allowed = {(roots.file_digest(raw), len(raw)) for raw in supplied.values()}
    require(all((ref['sha256'], ref['bytes']) in allowed for ref in proof['attachments']), 'artifact_gap')
    # Root pages and candidate originals are upstream of the proof.
    supplied = {**files.originals(), **supplied}
    proof_raw = a1.PROOF_MANIFEST.serialize(proof)
    supplied['proof_manifest'] = proof_raw
    manifest['proof_manifest_sha256'] = roots.file_digest(proof_raw)
    contract.validate_preimages(manifest, proof, artifacts=supplied, roots=preimages)
    from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
    graph = PayloadGraph(supplied)
    graph.materialize({k: contract._object_bytes(supplied[k], schema)
                       for k, schema in contract.OBJECT_PREIMAGES.items()})
    require(not graph.issues, 'artifact_gap')
    for path, raw in supplied.items():
        files.put(path, raw)
    files.obj(manifest['day'] + '/manifest-v2.json', manifest, a1.MANIFEST)
    return manifest, proof
