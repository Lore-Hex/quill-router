"""Round four record joins, original membership, and completion regressions."""
from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from tests.test_async_settle_shadow_v2_pack_accounting import exported, reference
from tests.test_async_settle_shadow_v2_pack_round3_budget import budget_capture
from tests.test_async_settle_shadow_v2_pack_round3_sources import procedure_capture
from trusted_router.async_settle_shadow_v2_pack_accounting_stream import (
    compare_export_accounting_streamed,
)
from trusted_router.async_settle_shadow_v2_pack_captured_budget import CapturedBudget
from trusted_router.async_settle_shadow_v2_pack_daypack import compare_export_accounting
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_pack_procedures import procedure_result
from trusted_router.detached_jws import canonical


@pytest.fixture(scope='module')
def capture(tmp_path_factory):
    from tests.shadow_v2_pack_round3_captures import writer_capture
    return writer_capture(tmp_path_factory.mktemp('round4-writer'))


@pytest.mark.parametrize('parser', ['regular', 'streamed'])
def test_missing_native_page_relabelled_with_all_references_rebuilt(tmp_path, parser):
    files, result = exported(tmp_path)
    root = json.loads(result.accounting)
    reads = json.loads(files.read(root['details']['reads'][0]))
    entries = json.loads(files.read(root['details']['entries'][0]))
    pages = reads['entries']
    pages.pop(1)
    pages[1]['after'] = pages[0]['last']
    for entry in entries['entries'][2:]:
        entry['metadata']['source'] = 'supplied_collector_original'
    for name, value in [('reads', reads), ('entries', entries)]:
        path = root['details'][name][0]['path']
        raw = canonical(value)
        (files.root / path).write_bytes(raw)
        root['details'][name][0] = reference(path, raw)
    root['source_operations'] = len(pages)
    root['operations'] = dict(native_rpcs=sum(p['native_rpcs'] for p in pages),
        pages=sum(p['source'] == 'rows' for p in pages), rows=sum(p['count'] for p in pages if p['source'] == 'rows'),
        objects=sum(p['count'] for p in pages if p['source'] == 'object'),
        body_bytes=sum(p.get('body_bytes', 0) for p in pages), elapsed_us=sum(p.get('elapsed_us', 0) for p in pages))
    raw = canonical(root)
    (files.root / 'accounting.json').write_bytes(raw)
    ref = reference('accounting.json', raw)
    if parser == 'regular':
        compared = compare_export_accounting(files, ref, result.transport_path.read_bytes(), day=root['day'], object_plan={})
    else:
        compared = compare_export_accounting_streamed(files, ref, result.transport_path, day=root['day'], object_plan={})
    assert not compared.complete
    assert 'native_member_label' in compared.missing
    assert any(s.startswith('range_') for s in compared.missing)


@pytest.mark.parametrize('damage', ['resource', 'date', 'boot', 'interval', 'shutdown_omitted',
    'claim_operations_per_second', 'scan_operations_per_second', 'publication_operations_per_second',
    'startup_operations_1s'])
def test_budget_original_population_and_peaks(tmp_path, capture, damage):
    files = PackFiles(tmp_path)
    plan = budget_capture(files, capture)
    if damage in ('resource', 'interval'):
        scope = json.loads(files.read(plan['post_scope']))
        scope['resource' if damage == 'resource' else 'start_us'] = 'other' if damage == 'resource' else scope['start_us'] + 1
        plan['post_scope'] = files.obj('changed/scope', scope)
    elif damage == 'shutdown_omitted':
        plan['shutdown'] = []
    elif damage in ('date', 'boot'):
        key = 'day' if damage == 'date' else 'boot'
        pair = plan['shutdown'][0]
        original = json.loads(files.read(pair['original']))
        measured = json.loads(files.read(pair['measurement']))
        original[key] = measured[key] = '2026-09-01' if damage == 'date' else 'other-boot'
        pair['original'] = files.obj('changed/span', original)
        measured['source'] = pair['original']
        pair['measurement'] = files.obj('changed/measurement', measured)
    else:
        for stage in ('pre', 'post'):
            plan[stage]['load_segments'][0]['derivations'][damage]['original'] = files.obj('changed/' + damage,
                dict(value=0 if damage.endswith('_1s') else dict(numerator=0, denominator=1)))
    with pytest.raises(ValueError):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')


def rewrite_procedures(files, plan, change):
    with zipfile.ZipFile(files.root / plan['archive']) as archive:
        output = json.loads(archive.read(plan['member']['name']))
    for name, ref in output['procedures'].items():
        value = json.loads(files.read(ref))
        change(value)
        output['procedures'][name] = files.obj('changed/' + name, value)
    raw = canonical(output)
    with zipfile.ZipFile(files.root / plan['archive'], 'w') as archive:
        archive.writestr(plan['member']['name'], raw)
    plan['archive_sha256'] = hashlib.sha256((files.root / plan['archive']).read_bytes()).hexdigest()
    plan['member'].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    plan['enclave_ci']['result'] = files.obj('changed/enclave-result', output)


@pytest.mark.parametrize('damage', ['generic', 'resource', 'configuration', 'enclave_missing', 'enclave_revision'])
def test_native_scope_and_both_revision_metadata(tmp_path, damage):
    files = PackFiles(tmp_path)
    plan = procedure_capture(files, 'fleet_load_budget', 'fleet-budgets-pre')
    if damage == 'enclave_missing':
        del plan['enclave_ci']
    elif damage == 'enclave_revision':
        job = json.loads(files.read(plan['enclave_ci']['job']))
        job['head_sha'] = 'b' * 40
        plan['enclave_ci']['job'] = files.obj('changed/job', job)
    else:
        def change(original):
            if damage == 'generic':
                original['kind'] = 'ci_procedure'
                for key in ('resource', 'configuration', 'operations'):
                    del original[key]
            elif damage == 'resource':
                original['resource'] = 'other-database'
            else:
                config = json.loads(files.read(original['configuration']))
                config['resource'] = 'other-database'
                original['configuration'] = files.obj('changed/config', config)
        rewrite_procedures(files, plan, change)
    with pytest.raises((ValueError, KeyError)):
        procedure_result(files, plan, capture_root=tmp_path)


@pytest.mark.parametrize('removed', ['closure', 'ledger'])
def test_independent_roots_keep_missing_records(tmp_path, capture, monkeypatch, removed):
    from scripts.async_settle import shadow_v2_window as window
    from tests.test_async_settle_shadow_v2_pack_round3_composition import stage_capture
    from trusted_router.async_settle_shadow_v2_pack_composition import compose_day
    source = tmp_path / 'capture'
    plan = stage_capture(source, capture)
    observed = {}
    def changed(files, **kwargs):
        producers = kwargs['producers']
        if removed == 'closure':
            producers['NativeExport']['rows'] = [r for r in producers['NativeExport']['rows'] if 'closure' not in r['kind']]
        else:
            producers['Readiness']['ledgers'] = []
        result = compose_day(files, **kwargs)
        observed.update(result)
        return result
    monkeypatch.setattr(window, 'compose_day', changed)
    result = window.collect_day(source, tmp_path / 'out', plan)
    assert not result['complete'] and not observed['manifest']['complete']
    root = 'manifest.closure_root' if removed == 'closure' else 'manifest.prerequisite_ledger_root'
    pages = sorted((tmp_path / 'out' / plan['day'] / 'roots' / root).glob('*.json'))
    entries = [e for p in pages for e in json.loads(p.read_bytes())['entries']]
    assert entries and any(e['state'] == 'missing' for e in entries)
    if removed == 'closure':
        assert list((tmp_path / 'out' / 'obligations' / plan['day']).rglob('00001.json'))


def test_repeat_full_composition_bytes(tmp_path, capture):
    from scripts.async_settle.shadow_v2_window import collect_day
    from tests.test_async_settle_shadow_v2_pack_round3_composition import stage_capture
    source = tmp_path / 'source'
    plan = stage_capture(source, capture)
    results = [collect_day(source, tmp_path / name, plan) for name in ('one', 'two')]
    assert results[0] == results[1]
    one = {p.relative_to(tmp_path / 'one'): p.read_bytes() for p in (tmp_path / 'one').rglob('*') if p.is_file()}
    two = {p.relative_to(tmp_path / 'two'): p.read_bytes() for p in (tmp_path / 'two').rglob('*') if p.is_file()}
    assert one == two


@pytest.mark.parametrize('damage', ['rerun', 'over_ceiling', 'reused_accounting', 'unlisted_bundle'])
def test_composed_pass_list_and_output_receipts(tmp_path, capture, damage):
    from scripts.async_settle.shadow_v2_window import collect_day
    from tests.test_async_settle_shadow_v2_pack import START, review
    from tests.test_async_settle_shadow_v2_pack_round3_composition import stage_capture
    from trusted_router.async_settle_shadow_v2_pack_daypack import bind_accounting
    source = tmp_path / 'source'
    plan = stage_capture(source, capture)
    files = PackFiles(source)
    native = json.loads(files.read(plan['captures']['NativeExport']))
    if damage == 'rerun':
        # The independent finite list still requires the deleted rerun.
        plan['pass_ids'].append('rerun')
    elif damage in ('reused_accounting', 'unlisted_bundle'):
        if damage == 'reused_accounting':
            plan['pass_ids'].append('rerun')
            native['passes'].append({**native['passes'][0], 'id': 'rerun'})
        else:
            raw = (source / native['bundle']).read_bytes()
            (source / 'unlisted-bundle.json').write_bytes(raw)
            native['bundle'] = 'unlisted-bundle.json'
        plan['captures']['NativeExport'] = files.obj('captures/changed-native', native)
        plan['files'].append(plan['captures']['NativeExport'])
    else:
        target = files.put('test-output', b'x')
        receipt = dict(object=target, at_us=START, operation='put', native_rpcs=10001,
                       bytes=1, elapsed_us=1, **{'pass': 1})
        native['output_receipts'] = [files.obj('test-output-receipt', receipt)]
        plan['captures']['NativeExport'] = files.obj('captures/changed-native', native)
        plan['files'].extend([target, native['output_receipts'][0], plan['captures']['NativeExport']])
        inventory = json.loads(files.read(plan['captures']['Inventory']))
        bound = bind_accounting(files, json.loads(files.read(inventory['deployment'])),
            day=plan['day'], accounting=[p['accounting'] for p in native['passes']])
        ref = files.obj('test-bound', bound)
        plan['deployment_accounting_review'] = review(files, ref, 'test-bound-review')
        plan['files'].append(plan['deployment_accounting_review']['record'])
    with pytest.raises(ValueError, match='FinitePassPlan|evidence_export_budget_gap'):
        collect_day(source, tmp_path / 'out', plan)


def test_closure_absent_from_captured_native_range(tmp_path, capture, monkeypatch):
    from scripts.async_settle.shadow_v2_window import collect_day
    from tests.test_async_settle_shadow_v2_export_native import Database, fake_http, native, reply
    from tests.test_async_settle_shadow_v2_pack_round3_composition import stage_capture
    from trusted_router.async_settle_shadow_v2_export import export_day
    keys = sorted(capture['objects'].refs)
    rows = [r for r in capture['bundle']['rows'] if 'closure' not in r['kind']]
    db = Database([(r['kind'], r['id'], r['raw']) for r in rows])
    fake_http(monkeypatch, [reply(capture['objects'].read(capture['objects'].refs[key])) for key in keys])
    export = export_day(native(db, page_size=2, plan=dict.fromkeys(keys, '7')), capture['bundle']['days'][0],
        completed_at_us=capture['bundle']['completed_at_us'], object_keys=keys, spool_dir=tmp_path / 'native')
    assert not export.gaps
    changed = {**capture, 'native_root': tmp_path / 'native'}
    source = tmp_path / 'source'
    plan = stage_capture(source, changed)
    result = collect_day(source, tmp_path / 'out', plan)
    assert not result['complete']
    assert any(value.startswith('closure:') for value in result['missing'])


def test_startup_burst_inside_long_interval_blocks(tmp_path, capture):
    files = PackFiles(tmp_path)
    plan = budget_capture(files, capture)
    item = plan['writer_operations'][0]
    raw = json.loads(files.read(item['operation_refs'][0]))
    startup = next(o for o in raw['operations'] if o['operation'] == 'startup')
    raw['operations'].append({**startup, 'id': 'second-startup', 'at_us': startup['at_us'] + 1})
    item['operation_refs'] = [files.obj('changed/burst', raw)]
    assert item['end_us'] - item['start_us'] > 4000000
    assert plan['pre']['load_segments'][0]['fields']['end_us'] == item['end_us']
    with pytest.raises(ValueError, match='MeasuredPeaks'):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')


def test_round4_matrix_changes_are_exact():
    from pathlib import Path
    old = json.loads(Path('tests/shadow_v2_pack_round3_base_results.json').read_bytes())
    new = json.loads(Path('tests/shadow_v2_pack_round4_base_results.json').read_bytes())
    assert set(old['cases']) == set(new['cases'])
    for case in old['cases']:
        before = old['vectors'][old['cases'][case]]
        after = new['vectors'][new['cases'][case]]
        removed = [f for f in before['failures'] if f not in after['failures']]
        added = [f for f in after['failures'] if f not in before['failures']]
        expected_added = []
        if case in ('accounting_file', 'false_shutdown_zero'):
            expected_added.extend(('evidence_schema_gap', 'publisher_trace',
                'publisher-traces/2026-10-12/11111111-1111-4111-8111-111111111111/' + slot)
                for slot in ('895881600', '895881601'))
        if case == 'accounting_file':
            expected_added.append(('artifact_gap', 'a2_file', 'native-export/accounting.json'))
        assert [(f['gap'], f['subject']['object']['kind'], f['subject']['object']['key'])
                for f in added] == expected_added
        assert all(f['count'] == 1 and f['rule'] == 'G1' for f in added)
        for failure in removed:
            if case == 'accounting_file' and failure['gap'] == 'artifact_gap':
                assert failure['count'] == 1 and failure['rule'] == 'G1'
                assert failure['subject']['object'] == dict(event_seq=None,
                    key='proof.external_proofs_sha256', kind='a2_file')
                continue
            assert failure['gap'] == 'evidence_schema_gap' and failure['rule'] == 'G1'
            assert failure['count'] == 1
            assert failure['subject']['object'] == dict(event_seq=None,
                key='manifest.independent_inventory_sha256', kind='a2_file')


def test_public_command_retains_incomplete_source_state(tmp_path, capsys):
    from scripts.async_settle.shadow_v2_window import main
    from trusted_router.async_settle_shadow_v2_pack_stage import PRODUCERS
    missing = dict(path='absent', bytes=2, sha256=hashlib.sha256(b'{}').hexdigest())
    path = tmp_path / 'plan.json'
    path.write_bytes(canonical(dict(stages=dict(pre=dict(captures=dict.fromkeys(PRODUCERS, missing))))))
    output = tmp_path / 'out'
    assert main(['collect', '--plan', str(path), '--stage', 'pre', '--out', str(output)]) == 1
    value = json.loads((output / 'stage/result.json').read_bytes())
    assert not value['complete'] and value['stage'] == 'pre'
    assert value['missing'] == ['MissingProducer:NativeExport:absent']
    assert main(['review-inputs', '--plan', str(path), '--input', str(output)]) == 1
    assert 'IncompleteCapture' in capsys.readouterr().out
