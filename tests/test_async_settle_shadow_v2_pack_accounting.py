"""Original-byte NativeExport comparisons, including re-hashed changed captures."""
from __future__ import annotations

import json

import pytest

from tests.test_async_settle_shadow_v2_export_native import DAY, RAW, Database, native
from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_export import DAY_KINDS, export_day
from trusted_router.async_settle_shadow_v2_pack_daypack import compare_export_accounting
from trusted_router.detached_jws import canonical


def reference(path, raw):
    return dict(path=path, sha256=file_digest(raw), bytes=len(raw))


def exported(tmp_path, count=4):
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = Database([(DAY_KINDS[0], DAY + f'/{i:04}', RAW.decode()) for i in range(count)])
    result = export_day(native(db, page_size=2), DAY, completed_at_us=1,
                        object_keys=(), spool_dir=tmp_path / 'spool')
    assert not result.gaps
    return Files(tmp_path / 'spool'), result


def test_original_native_export_accounting_is_complete(tmp_path):
    files, result = exported(tmp_path)
    found = compare_export_accounting(files, reference('accounting.json', result.accounting),
        result.transport_path.read_bytes(), day=DAY, object_plan={})
    assert found.complete, found.missing
    assert found.native_rpcs == 18
    assert found.body_bytes == len(RAW) * 4
    assert len(found.sources) == 8


@pytest.mark.parametrize('damage', ['middle_page', 'last_page', 'eof', 'kind', 'day',
    'snapshot', 'cursor', 'hash', 'byte', 'extra_rpc', 'operation_sum', 'body_sum',
    'failure', 'continuation', 'record', 'accounting'])
def test_accounting_each_original_change_blocks(tmp_path, damage):
    files, result = exported(tmp_path)
    root = json.loads(result.accounting)
    bundle = json.loads(result.transport_path.read_bytes())
    reads_ref = root['details']['reads'][0]
    reads_path = files.root / reads_ref['path']
    reads = json.loads(reads_path.read_bytes())
    pages = reads['entries']
    if damage == 'middle_page':
        del pages[1]
    elif damage in ('last_page', 'eof'):
        del pages[2]
    elif damage == 'kind':
        reads['entries'] = [p for p in pages if p['kind'] != DAY_KINDS[1]]
    elif damage == 'day':
        root['day'] = '2026-10-11'
    elif damage == 'snapshot':
        pages[-1]['snapshot'] += 1
    elif damage == 'cursor':
        pages[1]['after'] += 'changed'
    elif damage == 'hash':
        root['details']['entries'][0]['sha256'] = '0' * 64
    elif damage == 'byte':
        entries = json.loads(files.read(root['details']['entries'][0]))
        path = files.root / entries['entries'][0]['original']
        path.write_bytes(path.read_bytes() + b' ')
    elif damage == 'extra_rpc':
        pages[0]['native_rpcs'] += 1
    elif damage == 'operation_sum':
        root['source_operations'] -= 1
    elif damage == 'body_sum':
        root['source_body_bytes'] -= 1
    elif damage == 'failure':
        pages[0]['status'] = 'TimeoutError'
    elif damage == 'continuation':
        pages[-1]['continuation'] = DAY + '/unconsumed'
    elif damage == 'record':
        bundle['rows'].pop()
    elif damage == 'accounting':
        (files.root / 'accounting.json').unlink()
        with pytest.raises(OSError):
            compare_export_accounting(files, reference('accounting.json', result.accounting),
                canonical(bundle), day=DAY, object_plan={})
        return
    reads_raw = canonical(reads)
    reads_path.write_bytes(reads_raw)
    root['details']['reads'][0] = reference(reads_ref['path'], reads_raw)
    raw = canonical(root)
    (files.root / 'accounting.json').write_bytes(raw)
    if damage in ('hash', 'byte'):
        with pytest.raises(ValueError):
            compare_export_accounting(files, reference('accounting.json', raw), canonical(bundle), day=DAY, object_plan={})
    else:
        found = compare_export_accounting(files, reference('accounting.json', raw), canonical(bundle), day=DAY, object_plan={})
        assert not found.complete, damage


def test_accounting_bytes_deterministic(tmp_path):
    one_files, one = exported(tmp_path / 'a')
    two_files, two = exported(tmp_path / 'b')
    assert one.accounting == two.accounting
    assert one.transport_path.read_bytes() == two.transport_path.read_bytes()
    first = compare_export_accounting(one_files, reference('accounting.json', one.accounting),
                                     one.transport_path.read_bytes(), day=DAY, object_plan={})
    second = compare_export_accounting(two_files, reference('accounting.json', two.accounting),
                                      two.transport_path.read_bytes(), day=DAY, object_plan={})
    assert first == second


def test_production_requires_native_accounting(tmp_path):
    files, result = exported(tmp_path)
    root = json.loads(result.accounting)
    root['native'] = False
    raw = canonical(root)
    (files.root / 'changed-accounting').write_bytes(raw)
    found = compare_export_accounting(files, reference('changed-accounting', raw),
        result.transport_path.read_bytes(), day=DAY, object_plan={})
    assert 'native_provenance' in found.missing
