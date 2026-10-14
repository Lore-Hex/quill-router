"""Round-eight file population, time, and support regressions."""
from __future__ import annotations

import copy
import json
import shutil

import pytest

from scripts.async_settle.shadow_v2_window import (
    collect_day,
    write_composed_transport,
    write_transport,
)
from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_pack_round3_captures import writer_capture
from tests.shadow_v2_pack_round5_captures import captured_day
from tests.test_async_settle_shadow_v2_pack import DAY, START, review
from tests.test_async_settle_shadow_v2_pack_round3_budget import budget_capture
from tests.test_async_settle_shadow_v2_pack_round3_composition import stage_capture
from trusted_router.async_settle_shadow_v2_pack_captured_budget import CapturedBudget
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, check_plan
from trusted_router.detached_jws import canonical


@pytest.fixture(scope='module')
def writer(tmp_path_factory):
    return writer_capture(tmp_path_factory.mktemp('round8-writer'))


def test_r23_two_receipts_require_two_observations(tmp_path, writer):
    from trusted_router.async_settle_shadow_v2_pack_daypack import AccountingResult
    from trusted_router.async_settle_shadow_v2_pack_passes import measured_export_costs
    files = PackFiles(tmp_path)
    plan = budget_capture(files, writer)
    receipt = json.loads(files.read(plan['output_receipts'][0]))
    second = files.put('second-output', b'abc')
    plan['output_receipts'].append(files.obj('second-receipt', {**receipt, 'object': second, 'bytes': 3}))
    output = json.loads(files.read(plan['output_plan']))
    output['passes'][0]['outputs'].append(dict(object=second, operation='put'))
    plan['output_plan'] = files.obj('two-output-plan', output)
    plan['output_plan_review'] = review(files, plan['output_plan'], 'two-output-review')
    ceilings = json.loads(files.read(plan['pre_allowances']))
    plan['export_ceilings'] = files.obj('ceilings', {k: ceilings[k] for k in (
        'export_passes', 'export_operations', 'export_bytes', 'export_write_bytes')} | dict(native_rpcs=99))
    plan['export_review'] = review(files, plan['export_ceilings'], 'ceilings-review')
    costs = measured_export_costs(files, plan, [AccountingResult(DAY, (), (), 1, 1, 1)], plan['output_receipts'])
    assert costs['export_write_bytes'] == 4
    with pytest.raises(ValueError, match='ExportPopulation'):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')


def test_s52_both_close_lists_shortened_with_shutdown_retained(tmp_path, writer):
    files = PackFiles(tmp_path)
    plan = budget_capture(files, writer)
    item = plan['writer_operations'][0]
    operations = json.loads(files.read(item['operation_refs'][0]))
    operations['operations'] = [o for o in operations['operations'] if o['operation'] != 'close']
    item['operation_refs'] = [files.obj('changed/ops', operations)]
    record = json.loads(files.read(plan['trial_record']))
    population = json.loads(files.read(record['operation_population']))
    population['operations'] = [o for o in population['operations'] if o['operation'] != 'close']
    record['operation_population'] = files.obj('changed/pop', population)
    plan['trial_record'] = files.obj('changed/record', record)
    plan['trial_review'] = review(files, plan['trial_record'], 'changed/review')
    for stage in ('pre', 'post'):
        plan[stage]['load_segments'][0]['derivations']['close_operations_1s'] = dict(kind='measured',
            original=files.obj('changed/close-peak', dict(value=0)), field=['value'])
    assert plan['shutdown']
    with pytest.raises(ValueError, match='ClosePopulation'):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')


def test_r26_publisher_time_must_belong_to_slot_and_role(tmp_path, writer):
    from trusted_router.async_settle_shadow_v2_pack_measurements import operation_join
    files = PackFiles(tmp_path)
    plan = budget_capture(files, writer)
    item = plan['writer_operations'][0]
    start, end = item['start_us'], item['end_us']
    roles = [dict(start_us=start, end_us=start + 500000, contender=True),
        dict(start_us=start + 500000, end_us=start + 1500000, contender=False),
        dict(start_us=start + 1500000, end_us=end, contender=True)]
    operation_join(files, **item, role_intervals=roles)
    body = json.loads(files.read(item['operation_refs'][0]))
    moved = 0
    for operation in body['operations']:
        if operation['operation'] in ('claims', 'scans', 'publications') and operation['at_us'] == start:
            operation['at_us'] = start + 1000000
            moved += 1
    assert moved == 3
    item['operation_refs'] = [files.obj('changed/operations', body)]
    with pytest.raises(ValueError, match='PublisherOperationInterval'):
        operation_join(files, **item, role_intervals=roles)


@pytest.fixture(scope='module')
def support_sources(tmp_path_factory):
    root = tmp_path_factory.mktemp('round8-support')
    plans = {}
    for day in ('2026-10-12', '2026-10-13', '2026-10-14'):
        plan = captured_day(root / day, day)
        plan['source_root'] = day + '/capture'
        plans[day] = plan
        files = PackFiles(root / plan['source_root'])
        capture = json.loads(files.read(plan['captures']['NativeExport']))
        path = files.root / capture['bundle']
        bundle = json.loads(path.read_bytes())
        bundle['completed_at_us'] = plan['complete_through_us']
        path.write_bytes(canonical(bundle))
        completion = json.loads(files.read(plan['completion']))
        completion['completed_at_us'] = plan['complete_through_us'] + 1
        old = plan['completion']
        plan['completion'] = files.obj('captures/later-completion', completion)
        plan['files'] = [plan['completion'] if ref == old else ref for ref in plan['files']]
    return root, plans


@pytest.mark.parametrize('damage', ['accounting', 'range'])
def test_r24_support_comparison_precedes_inner_completion(tmp_path, support_sources, damage):
    from trusted_router.async_settle_shadow_v2_pack_window import reconciliation
    root, original = support_sources
    shutil.copytree(root, tmp_path / 'source')
    root = tmp_path / 'source'
    plans = copy.deepcopy(original)
    spec = plans['2026-10-13']
    files = PackFiles(root / spec['source_root'])
    capture = json.loads(files.read(spec['captures']['NativeExport']))
    if damage == 'accounting':
        (files.root / capture['accounting']['path']).unlink()
    else:
        from tests.test_async_settle_shadow_v2_pack_accounting import reference
        accounting = json.loads(files.read(capture['accounting']))
        ref = accounting['details']['reads'][0]
        path = files.root / (capture['prefix'] + ref['path'])
        page = json.loads(path.read_bytes())
        page['entries'].pop(0)
        path.write_bytes(canonical(page))
        accounting['details']['reads'][0] = reference(ref['path'], canonical(page))
        path = files.root / capture['accounting']['path']
        path.write_bytes(canonical(accounting))
        changed = reference(capture['accounting']['path'], canonical(accounting))
        capture['accounting'] = capture['passes'][0]['accounting'] = changed
        # Rebuild the referring capture and registration, keeping the independent pass plan.
        path = files.root / spec['captures']['NativeExport']['path']
        path.write_bytes(canonical(capture))
        spec['captures']['NativeExport'] = reference(path.relative_to(files.root).as_posix(), canonical(capture))
        from tests.shadow_v2_pack_round8_work import rebuild_referring_records
        rebuild_referring_records(files, spec)
        from trusted_router.async_settle_shadow_v2_pack_passes import compare_source_registrations
        compare_source_registrations(files, spec)
    joined = reconciliation(root, plans, selected=[DAY])
    assert joined.day_status['2026-10-13'] is False
    result = collect_day(root / plans[DAY]['source_root'], tmp_path / 'out', plans[DAY], reconciliation=joined)
    for path in (DAY + '/manifest-v2.json', 'proof_manifest'):
        assert json.loads((tmp_path / 'out' / path).read_bytes())['complete'] is False
    assert 'IncompleteSupportDay:2026-10-13' in result['missing']


def test_s51_empty_references_do_not_define_output_population(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_output_costs import reserve_files
    from trusted_router.async_settle_shadow_v2_pack_passes import compare_later_files
    files = PackFiles(tmp_path)
    ref = files.put('gate/00000.json', b'report')
    reservation = reserve_files([ref], source_pass=1,
        ceiling=dict(export_operations=1, export_bytes=6, export_write_bytes=6, native_rpcs=0))
    output = files.obj('output-plan', dict(passes=[{'pass': 1}], later_passes=[dict(
        export_operations=1, export_bytes=6, export_write_bytes=6, native_rpcs=0)]))
    reserved = files.obj('individual-plan', reservation)
    plan = dict(output_plan=output, later_file_plan=reserved,
        later_file_review=review(files, reserved, 'individual-review'))
    assert compare_later_files(files, plan, [ref])['measured']['export_operations'] == 1
    with pytest.raises(ValueError, match='LaterOutputPopulation'):
        compare_later_files(files, plan, [])


def test_fixed_release_files_and_composed_completion_real_loader(tmp_path, writer):
    from tests.shadow_v2_pack_round3_matrix import stage_ports
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
    source = tmp_path / 'source'
    plan = stage_capture(source, writer)
    result = collect_day(source, tmp_path / 'out', plan)
    bundle = json.loads((tmp_path / 'out/stage/transport.json').read_bytes())
    def fixed_name(path):
        return path in RELEASE_FILES or any(path.startswith('released/' + name + '/') for name in RELEASE_FILES)
    assert not any(fixed_name(p['name']) for p in bundle['preimages'])
    files = PackFiles(tmp_path / 'out')
    assert not any(fixed_name(r['path']) for r in check_plan(files, result['inputs']))
    ports = stage_ports(bundle)
    from dataclasses import replace

    from trusted_router.async_settle_shadow_v2_gate_bundle import decode_bundle
    read = ports.read_release_file
    seen = []
    def reader(path):
        seen.append(path)
        return read(path)
    ports = replace(ports, read_release_file=reader)
    loaded = load(canonical(bundle), ports)
    context = decode_bundle(loaded, ports, 'transport')
    assert set(seen) == set(RELEASE_FILES.values())
    assert set(context.release_files) == set(RELEASE_FILES)
    changed = replace(ports, read_release_file=lambda path: reader(path) + b' ')
    bad = decode_bundle(load(canonical(bundle), changed), changed, 'transport')
    assert not bad.release_files
    assert sum('release checkout file differs' in issue.detail for issue in bad.issues) == 3
    assert not any('exported FILE_REF byte bound' in str(issue) for issue in loaded.a2_streams['loading_issues'])
    assert bundle['completed_at_us'] == START + 86400000010
    assert writer['bundle']['completed_at_us'] < bundle['completed_at_us']
    assert all(key in json.loads(bundle['proof']) for key in (k.split('.')[1] for k in RELEASE_FILES))
    # The fixed checkout reader remains active; changing its returned bytes changes the load result.
    assert not any('shutdown_inventory' in str(issue) and 'schema' in str(issue) for issue in loaded.a2_streams['loading_issues'])


def test_transport_rewrite_requires_later_final_completion(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_originals import DayFiles
    files = DayFiles(tmp_path)
    files.obj('review', dict(reviewed_at_us=20))
    ref = files.obj('completion', dict(command='pack', completed_at_us=30, complete=True))
    original = tmp_path / 'source.json'
    write_composed_transport(files, dict(completed_at_us=10, rows=[], attachments=[], ledgers=[]), original)
    write_transport(files, original, tmp_path / 'final.json', completion_ref=ref)
    assert json.loads((tmp_path / 'final.json').read_bytes())['completed_at_us'] == 30
    early = files.obj('early', dict(command='pack', completed_at_us=10, complete=True))
    with pytest.raises(ValueError, match='ComposedCompletion'):
        write_transport(files, original, tmp_path / 'early.json', completion_ref=early)


def test_r25_complete_collection_file_population(tmp_path, writer):
    from trusted_router.async_settle_shadow_v2_pack_output_costs import (
        directory_references,
        reserve_files,
    )
    source = tmp_path / 'source'
    plan = stage_capture(source, writer)
    collect_day(source, tmp_path / 'shape', plan)
    references = directory_references(tmp_path / 'shape')
    # A separate reviewed pass reserves the prior known shape with finite byte headroom.
    reserved = reserve_files([{**r, 'bytes': r['bytes'] + 8192} for r in references], source_pass=1,
        ceiling=dict(export_operations=len(references), export_bytes=100000000,
            export_write_bytes=100000000, native_rpcs=0))
    files = PackFiles(source)
    ref = files.obj('reviewed-file-plan', reserved)
    plan['file_output'] = dict(later_file_plan=ref, later_file_review=review(files, ref, 'reviewed-file-review'))
    result = collect_day(source, tmp_path / 'out', plan)
    assert result['later_output_costs'] is not None, result['missing']
    costs = result['later_output_costs']
    actual = directory_references(tmp_path / 'out')
    assert len(costs['receipts']) == len(actual)
    assert {r['object']['path'] for r in costs['receipts']} == {r['path'] for r in actual}
    assert len([r for r in actual if r['path'].startswith('gate/')]) == 8
    assert DAY + '/manifest-v2.json' in {r['path'] for r in actual}
    assert json.loads((tmp_path / result['output_record']['path']).read_bytes()) == costs


@pytest.mark.parametrize('damage', ['missing', 'unreserved', 'oversized', 'report'])
def test_complete_output_pass_file_checks(tmp_path, damage):
    from trusted_router.async_settle_shadow_v2_pack_output_costs import reserve_files
    from trusted_router.async_settle_shadow_v2_pack_outputs import OutputPass
    source, out = tmp_path / 'source', tmp_path / 'out'
    source.mkdir()
    out.mkdir()
    names = ['transport.json', 'inputs/page.json', 'gate/output.json', 'gate/result.json',
        *[f'gate/{i:05d}.json' for i in range(6)], 'manifest.json', 'status.json']
    refs = [dict(path=name, bytes=4096) for name in names]
    reservation = reserve_files(refs, source_pass=1,
        ceiling=dict(export_operations=len(names), export_bytes=100000, export_write_bytes=100000, native_rpcs=0))
    if damage == 'report':
        reservation['reservations'] = [r for r in reservation['reservations'] if r['path'] != 'gate/00005.json']
    files = PackFiles(source)
    ref = files.obj('plan', reservation)
    spec = dict(file_output=dict(later_file_plan=ref, later_file_review=review(files, ref)))
    output = OutputPass.begin(source, out, spec)
    for name in names[:-1]:
        path = out / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'{}')
    if damage == 'missing':
        (out / 'gate/00000.json').unlink()
    elif damage == 'unreserved':
        (out / 'extra').write_bytes(b'x')
    elif damage == 'oversized':
        (out / 'transport.json').write_bytes(b'x' * 4097)
    result = output.finish(dict(complete=True, missing=[]), status='status.json')
    assert result['complete'] is False and result['later_output_costs'] is None
    assert ('evidence_export_budget_gap' if damage == 'oversized' else 'LaterOutputPopulation') in result['missing']


def test_following_pass_requires_exact_prior_record(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_output_costs import reserve_files
    from trusted_router.async_settle_shadow_v2_pack_outputs import OutputPass
    files = PackFiles(tmp_path / 'source')
    reservation = reserve_files([dict(path='status.json', bytes=1000)], source_pass=2,
        ceiling=dict(export_operations=1, export_bytes=1000, export_write_bytes=1000, native_rpcs=0))
    ref = files.obj('plan', reservation)
    output = OutputPass.begin(files.root, tmp_path / 'out', dict(file_output=dict(
        later_file_plan=ref, later_file_review=review(files, ref))))
    assert output.missing == 'FollowingOutputPopulation'


def test_declared_seven_days_do_not_replace_platform_coverage(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_schedule import continuous_coverage
    files = PackFiles(tmp_path)
    cohort = files.obj('cohort', dict(required_regions=['region']))
    plans = {}
    for index in range(8):
        ref = files.obj(f'platform/{index}', dict(boots=[dict(role='router', region='region',
            start_us=index * 86400000000, end_us=index * 86400000000 + 4100000)]))
        plans[str(index)] = dict(captures=dict(Inventory=ref), cohort=cohort)
    interval = dict(start_us=4000000, end_us=604804000000)
    assert continuous_coverage(tmp_path, plans, interval) == ['WindowWriterCoverage:region']
    for index, spec in enumerate(plans.values()):
        spec['captures']['Inventory'] = files.obj(f'continuous/{index}', dict(boots=[dict(
            role='router', region='region', start_us=index * 86400000000, end_us=(index + 1) * 86400000000)]))
    assert continuous_coverage(tmp_path, plans, interval) == []


def test_public_collect_has_one_complete_output_record(tmp_path, writer):
    from scripts.async_settle.shadow_v2_window import window
    from trusted_router.async_settle_shadow_v2_pack_output_costs import (
        directory_references,
        reserve_files,
    )
    source = tmp_path / 'source'
    selected = stage_capture(source, writer)
    plan = dict(default_stage='pre', stages=dict(pre=selected))
    window('collect', plan, source=source, out=tmp_path / 'shape')
    references = directory_references(tmp_path / 'shape')
    reservations = reserve_files([{**r, 'bytes': r['bytes'] + 8192} for r in references], source_pass=1,
        ceiling=dict(export_operations=len(references), export_bytes=100000000,
            export_write_bytes=100000000, native_rpcs=0))
    files = PackFiles(source)
    ref = files.obj('public-output-plan', reservations)
    selected['output_passes'] = dict(collect=dict(file_output=dict(later_file_plan=ref,
        later_file_review=review(files, ref, 'public-output-review'))))
    result = window('collect', plan, source=source, out=tmp_path / 'out')
    assert result['later_output_costs'] is not None, result['missing']
    assert len(result['later_output_costs']['receipts']) == len(directory_references(tmp_path / 'out'))
    assert len(list(tmp_path.glob('out.output-receipt.json'))) == 1
