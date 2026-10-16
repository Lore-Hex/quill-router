"""Selected accounting and lifecycle comparisons with rebuilt referring records."""
from __future__ import annotations

import copy
import json
import shutil

import pytest

from scripts.async_settle.shadow_v2_window import collect_day
from tests.shadow_v2_gate_cache import payloadgraph_shared_lookup_cache  # noqa: F401
from tests.shadow_v2_pack_round3_captures import writer_capture
from tests.shadow_v2_pack_round9_lifecycle import refresh_population
from tests.test_async_settle_shadow_v2_pack import DAY, review
from tests.test_async_settle_shadow_v2_pack_round3_budget import budget_capture
from tests.test_async_settle_shadow_v2_pack_round3_composition import stage_capture
from trusted_router.async_settle_shadow_v2_pack_captured_budget import CapturedBudget
from trusted_router.async_settle_shadow_v2_pack_originals import DayFiles
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.detached_jws import canonical


@pytest.fixture(scope='module')
def writer(tmp_path_factory):
    return writer_capture(tmp_path_factory.mktemp('round9-writer'))


@pytest.fixture(scope='module')
def composed(tmp_path_factory, writer):
    root = tmp_path_factory.mktemp('round9-composed')
    return root, stage_capture(root, writer)


def no_measurements(root):
    assert not (root / 'proof.pre_budget_sha256').exists()
    assert not (root / 'proof.post_budget_sha256').exists()
    # Stopping before assembly emits neither inner record, hence no true inner flag.
    for name in (DAY + '/manifest-v2.json', 'proof_manifest'):
        path = root / name
        assert (json.loads(path.read_bytes())['complete'] if path.exists() else False) is False


@pytest.mark.parametrize('damage,diagnostic', [('total', 'initial:operation_totals'), ('range', 'initial:')])
def test_selected_accounting_stops_before_measurement(tmp_path, writer, damage, diagnostic):
    from tests.test_async_settle_shadow_v2_pack_accounting import reference
    from trusted_router.async_settle_shadow_v2_pack_passes import (
        compare_passes,
        compare_source_registrations,
    )
    capture = dict(writer)
    capture['native_root'] = tmp_path / 'native'
    shutil.copytree(writer['native_root'], capture['native_root'])
    path = capture['native_root'] / 'accounting.json'
    accounting = json.loads(path.read_bytes())
    if damage == 'total':
        accounting['operations']['native_rpcs'] += 1
    else:
        ref = accounting['details']['reads'][0]
        page_path = capture['native_root'] / ref['path']
        page = json.loads(page_path.read_bytes())
        page['entries'].pop(0)
        page_path.write_bytes(canonical(page))
        accounting['details']['reads'][0] = reference(ref['path'], canonical(page))
    path.write_bytes(canonical(accounting))
    # The normal builders create all references, timing records and reviews after the change.
    source, out = tmp_path / 'capture', tmp_path / 'out'
    plan = stage_capture(source, capture)
    files = PackFiles(source)
    compare_source_registrations(files, plan)
    native = json.loads(files.read(plan['captures']['NativeExport']))
    _, _, missing = compare_passes(files, plan, native)
    assert missing and any(diagnostic in item for item in missing)
    with pytest.raises(ValueError) as error:
        collect_day(source, out, plan)
    assert str(error.value) == ','.join(missing)
    no_measurements(out)
    assert not (out / 'budget/export-compared-totals.json').exists()


def changed_work(files, plan, change):
    record = json.loads(files.read(plan['trial_record']))
    item = plan['writer_operations'][0]
    operations = json.loads(files.read(item['operation_refs'][0]))
    for operation in operations['operations']:
        change(operation, item)
    item['operation_refs'] = [files.obj('changed/operations', operations)]
    work = json.loads(files.read(record['lifecycle_work']))
    work['operations'] = [dict(day=operations['day'], boot=operations['boot'], **o)
        for o in operations['operations'] if o['operation'] in ('startup', 'close')]
    record['lifecycle_work'] = files.obj('changed/work', work)
    refresh_population(files, plan, record)
    return record


@pytest.mark.parametrize('mode', ['baseline', 'zero', 'move', 'both', 'transaction', 'startup', 'close_zero'])
def test_r28_original_spans_and_costs(tmp_path, composed, mode):
    source, selected = composed
    shutil.copytree(source, tmp_path / 'capture')
    files = DayFiles(tmp_path / 'capture')
    plan = json.loads(files.read(selected['captures']['Budget']))
    record = json.loads(files.read(plan['trial_record']))
    originals = [files.read(s['original']) for s in plan['shutdown']]
    calls = files.read(record['lifecycle_calls'])
    def change(operation, item):
        if operation['operation'] == 'close':
            if mode in ('zero', 'both') or (mode == 'close_zero' and operation['id'] == 'close'):
                operation.update(rows=0, bytes=0, native_rpcs=0, elapsed_us=0)
            if mode in ('move', 'both'):
                operation['at_us'] = item['start_us']
            if mode == 'transaction':
                operation['native_rpcs'] = 1
        if mode == 'startup' and operation['id'] == 'startup':
            operation['native_rpcs'] = 0
    changed_work(files, plan, change)
    if mode == 'baseline':
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')
        operations = json.loads(files.read(plan['writer_operations'][0]['operation_refs'][0]))['operations']
        close = next(o for o in operations if o['id'] == 'close')
        assert (close['rows'], close['bytes'], close['native_rpcs']) == (6, 600, 3)
        result = next(o for o in operations if o['id'] == 'result')
        assert result['elapsed_us'] == 100
        span = json.loads(originals[0])
        assert span['spans']['result'] == [900, 1000]
    else:
        with pytest.raises(ValueError, match='Lifecycle(Cost|Time)'):
            CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')
        no_measurements(files.root)
    assert [files.read(s['original']) for s in plan['shutdown']] == originals
    assert files.read(record['lifecycle_calls']) == calls


@pytest.mark.parametrize('damage', ['missing_calls', 'missing_clock', 'span', 'call_span'])
def test_lifecycle_original_membership(tmp_path, writer, damage):
    files = PackFiles(tmp_path)
    plan = budget_capture(files, writer)
    record = json.loads(files.read(plan['trial_record']))
    work = json.loads(files.read(record['lifecycle_work']))
    if damage == 'missing_calls':
        del record['lifecycle_calls']
        expected = 'MissingLifecycleCalls'
    else:
        native = json.loads(files.read(record['lifecycle_calls']))
        if damage == 'missing_clock':
            platform = json.loads(files.read(record['platform']))
            del platform['boots'][0]['clock_origin']
            record['platform'] = files.obj('changed/platform', platform)
            native['platform'] = work['platform'] = record['platform']
            native['phases'][0]['source'] = record['platform']
            expected = 'LifecycleClock'
        else:
            phase = next(p for p in native['phases'] if p['id'] == 'result')
            phase['calls'][0]['span'] = [n - 1 for n in phase['span']]
            expected = 'LifecycleCallSpan'
            if damage == 'span':
                phase['span'] = phase['calls'][0]['span']
                record = changed_work(files, plan, lambda o, _: o.update(at_us=o['at_us'] - 1)
                    if o['id'] == 'result' else None)
                work = json.loads(files.read(record['lifecycle_work']))
                expected = 'LifecycleSpan'
        record['lifecycle_calls'] = files.obj('changed/calls', native)
        record['lifecycle_work'] = files.obj('changed/work2', work)
    refresh_population(files, plan, record)
    with pytest.raises(ValueError, match=expected):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')
    no_measurements(tmp_path)


def test_s39_shutdown_duration_with_consistent_work_reference(tmp_path, writer):
    files = PackFiles(tmp_path)
    plan = budget_capture(files, writer)
    original = files.read(plan['shutdown'][0]['original'])
    assert json.loads(original)['spans']['result'][1] - json.loads(original)['spans']['result'][0] == 2000000
    measured = json.loads(files.read(plan['shutdown'][0]['measurement']))
    measured['result_us'] = 0
    plan['shutdown'][0]['measurement'] = files.obj('changed/measurement', measured)
    record = json.loads(files.read(plan['trial_record']))
    record['shutdown'] = plan['shutdown']
    work = json.loads(files.read(record['lifecycle_work']))
    work['shutdown'] = plan['shutdown']
    record['lifecycle_work'] = files.obj('changed/work', work)
    refresh_population(files, plan, record)
    with pytest.raises(ValueError, match='^artifact_gap$'):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')
    assert files.read(plan['shutdown'][0]['original']) == original
    no_measurements(tmp_path)


def test_s37_recipe_capture_is_not_a_producer(tmp_path, composed):
    root, selected = composed
    plan = copy.deepcopy(selected)
    removed = plan['captures'].pop('Budget')
    plan['assembly'] = dict(captures={'Budget': removed})
    with pytest.raises(ValueError, match='^MissingProducer:Budget$'):
        collect_day(root, tmp_path / 'out', plan)
    no_measurements(tmp_path / 'out')


def test_following_original_bytes(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_output_costs import (
        compare_files,
        following_totals,
        reserve_files,
    )
    files = PackFiles(tmp_path)
    ref = files.put('original', b'abc')
    plan = reserve_files([ref], source_pass=1,
        ceiling=dict(export_operations=1, export_bytes=3, export_write_bytes=3, native_rpcs=0))
    record = compare_files(files, plan, [ref])
    (tmp_path / 'original').write_bytes(b'xyz')
    with pytest.raises(ValueError, match='LaterOutputBytes'):
        following_totals(files, [record], current_pass=3)


def test_completion_after_review(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_completion import completion
    files = PackFiles(tmp_path)
    files.obj('original', dict(reviewed_at_us=20))
    ref = files.obj('completed', dict(command='pack', complete=True, completed_at_us=19))
    with pytest.raises(ValueError, match='ComposedCompletion'):
        completion(files, ref, previous_us=10, command='pack')


def test_explicit_empty_export_population(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_work import export_work
    files = PackFiles(tmp_path)
    ref = files.obj('plan', dict(passes=[]))
    plan = dict(output_plan=ref, output_plan_review=review(files, ref), output_receipts=[])
    with pytest.raises(ValueError, match='ExportPassPopulation'):
        list(export_work(files, plan))


def test_short_lifetime_remains_incomplete(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_schedule import continuous_coverage
    files = PackFiles(tmp_path)
    cohort = files.obj('cohort', dict(required_regions=['r']))
    platform = files.obj('platform', dict(boots=[dict(role='router', region='r', start_us=0, end_us=4)]))
    assert continuous_coverage(tmp_path, dict(day=dict(captures=dict(Inventory=platform), cohort=cohort)),
        dict(start_us=0, end_us=100)) == ['WindowWriterCoverage:r']
