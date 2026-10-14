"""Streamed NativeExport comparisons and original-byte loader defect vectors."""
from __future__ import annotations

import copy
import json
import tracemalloc

import pytest

from tests import test_async_settle_shadow_v2_pack_accounting as previous
from tests.test_async_settle_shadow_v2_export_native import DAY, Database, native
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_export import DAY_KINDS, export_day
from trusted_router.async_settle_shadow_v2_pack_accounting_stream import (
    compare_export_accounting_streamed,
)
from trusted_router.detached_jws import canonical


@pytest.mark.parametrize('damage', ['middle_page', 'last_page', 'eof', 'kind', 'day',
    'snapshot', 'cursor', 'hash', 'byte', 'extra_rpc', 'operation_sum', 'body_sum',
    'failure', 'continuation', 'record', 'accounting'])
def test_streamed_accounting_same_original_negatives(monkeypatch, tmp_path, damage):
    def streamed(files, reference, bundle, **kwargs):
        path = tmp_path / 'streamed-capture.json'
        path.write_bytes(bundle)
        return compare_export_accounting_streamed(files, reference, path, **kwargs)
    monkeypatch.setattr(previous, 'compare_export_accounting', streamed)
    previous.test_accounting_each_original_change_blocks(tmp_path, damage)


def test_streamed_accounting_large_day_memory(tmp_path):
    raw = canonical(dict(original='x' * 250000)).decode()
    db = Database([(DAY_KINDS[0], DAY + f'/{i:04d}', raw) for i in range(128)])
    exported = export_day(native(db, page_size=2), DAY, completed_at_us=1,
                          object_keys=(), spool_dir=tmp_path / 'native')
    assert not exported.gaps
    assert exported.transport_path.stat().st_size > 30 * 1024 * 1024
    tracemalloc.start()
    try:
        found = compare_export_accounting_streamed(Files(tmp_path / 'native'),
            previous.reference('accounting.json', exported.accounting), exported.transport_path,
            day=DAY, object_plan={})
        _, peak = tracemalloc.get_traced_memory()
        assert found.complete, found.missing
        assert peak < 6 * 1024 * 1024, peak
    finally:
        tracemalloc.stop()


async def test_real_writer_loader_original_deletion_matrix_is_base_defect(monkeypatch, tmp_path):
    from tests import test_async_settle_shadow_v2_pack_integration as integration
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput, render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    await integration.test_real_writers_native_export_collect_and_pack_loader(monkeypatch, tmp_path)
    bundle = json.loads((tmp_path / 'native/bundle.json').read_bytes())
    accounting = json.loads((tmp_path / 'native/accounting.json').read_bytes())
    files = Files(tmp_path / 'native')
    root_ref = previous.reference('accounting.json', canonical(accounting))
    originals = {'export/accounting.json': canonical(accounting)}
    for refs in accounting['details'].values():
        for ref in refs:
            originals[ref['path']] = files.read(ref)
    bundle['preimages'] = [dict(name=name, raw=raw.decode()) for name, raw in originals.items()]
    vectors = {}
    for kind in DAY_KINDS:
        if kind.endswith('control_v2'):
            continue  # The cap must remain to document today's base result.
        changed = copy.deepcopy(bundle)
        changed['rows'] = [row for row in changed['rows'] if row['kind'] != kind]
        vectors['omit-kind-' + kind] = changed
    for name in originals:
        changed = copy.deepcopy(bundle)
        changed['preimages'] = [p for p in changed['preimages'] if p['name'] != name]
        vectors['omit-source-' + name] = changed
    for damage in ('middle_page', 'last_page', 'eof', 'day', 'snapshot', 'extra_rpc', 'rerun'):
        changed = copy.deepcopy(bundle)
        root = copy.deepcopy(accounting)
        reads_ref = root['details']['reads'][0]
        reads = json.loads(files.read(reads_ref))
        rows = reads['entries']
        if damage in ('middle_page', 'last_page', 'eof'):
            del rows[1 if damage == 'middle_page' else -1]
        elif damage == 'day':
            root['day'] = '2026-10-11'
        elif damage == 'snapshot':
            rows[-1]['snapshot'] += 1
        elif damage == 'extra_rpc':
            rows[0]['native_rpcs'] += 1
        else:
            root['passes'] += 1
        changed_reads = canonical(reads)
        root['details']['reads'][0] = previous.reference(reads_ref['path'], changed_reads)
        for item in changed['preimages']:
            if item['name'] == reads_ref['path']:
                item['raw'] = changed_reads.decode()
            if item['name'] == 'export/accounting.json':
                item['raw'] = canonical(root).decode()
        vectors['changed-' + damage] = changed
    assert len(vectors) >= 17
    for case, (name, value) in enumerate(vectors.items()):
        # Save every exact negative byte input for inspection before loading it.
        raw = canonical(value)
        target = tmp_path / 'negatives' / (name.replace('/', '_') + '.json')
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(raw)
        loaded = load(raw)
        assert loaded.a2_streams['loading_issues'] == [dict(gap='evidence_schema_gap',
            path='transport/container', detail="'router_writers'")], name
        from tests.shadow_v2_pack_round5_vectors import corrected_result
        corrected = corrected_result('test_real_writer_loader_original_deletion_matrix_is_base_defect', case)
        assert corrected['rows'] == len(value['rows'])
        assert len(corrected['loading_validation']) == [36, 42, 36, 40, 41, 41, *([42] * 11)][case]
        result = render(evaluate(loaded, gate='transport'))
        assert isinstance(result, UnavailableGateOutput), name
    # This is the present-base diagnostic matrix, not rendered §7.5 counts.
    assert root_ref['path'] == 'accounting.json'
