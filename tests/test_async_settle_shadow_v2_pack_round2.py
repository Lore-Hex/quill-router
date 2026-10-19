"""Round-two captured-source, bounded-memory and current-base regressions."""
from __future__ import annotations

import copy
import hashlib
import json
import tracemalloc
import zipfile

import pytest

from tests.test_async_settle_shadow_v2_pack import DAY, START, peaks
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, check_plan
from trusted_router.async_settle_shadow_v2_pack_scope import CHECK_SCOPE, check_scope, scoped_result
from trusted_router.async_settle_shadow_v2_pack_sources import (
    archive_members,
    compare_cpu,
    derived_peaks,
    monitoring_series,
    publisher_projection,
    timestamp_ns,
)
from trusted_router.async_settle_shadow_v2_pack_streaming import OriginalSpool, split_transport
from trusted_router.detached_jws import canonical


def monitoring(files, name, values=(.2, .21), *, minute=0):
    refs = []
    for index, value in enumerate(values):
        point = dict(interval=dict(startTime=f'2026-10-12T00:{minute:02d}:{index:02d}.000000001Z',
                                   endTime=f'2026-10-12T00:{minute:02d}:{index + 1:02d}.000000001Z'),
                     value=dict(doubleValue=value))
        series = dict(metric=dict(type='cpu', labels={}), resource=dict(type='spanner_instance', labels=dict(node='one')),
                      metricKind='GAUGE', valueType='DOUBLE', points=[point])
        original = files.obj(f'{name}/{index}', dict(timeSeries=[series], nextPageToken='next' if index == 0 else ''))
        refs.append(dict(page_token='' if index == 0 else 'next', original=original))
    return refs


def parse_monitoring(files, pages, minute=0):
    start = timestamp_ns(f'2026-10-12T00:{minute:02d}:00.000000001Z')
    return monitoring_series(files, pages, metric='cpu', metric_labels={},
        resource=dict(type='spanner_instance', labels=dict(node='one')),
        start_ns=start, end_ns=start + 2000000000, interval_ns=1000000000)


def test_native_monitoring_pages_and_baseline_trial(tmp_path):
    files = PackFiles(tmp_path)
    pages = monitoring(files, 'base')
    baseline = parse_monitoring(files, pages)
    trial = parse_monitoring(files, monitoring(files, 'trial', (.22, .25), minute=1), minute=1)
    result = compare_cpu(baseline, trial, dict(numerator=5, denominator=1))
    assert result['measured_increase'] == dict(numerator=4, denominator=1)
    assert baseline['samples'][0]['start_ns'] % 1000000000 == 1
    assert baseline['originals'] == pages
    assert baseline['samples'][0]['point']['interval']['startTime'].endswith('000000001Z')


@pytest.mark.parametrize('damage', ['missing', 'node', 'token', 'duplicate', 'interval', 'error', 'cpu'])
def test_monitoring_missing_and_changed_samples_block(tmp_path, damage):
    files = PackFiles(tmp_path)
    pages = monitoring(files, 'base')
    if damage == 'missing':
        pages.pop()
    elif damage == 'token':
        pages[1]['page_token'] = 'wrong'  # noqa: S105
    else:
        value = json.loads(files.read(pages[1]['original']))
        series = value['timeSeries'][0]
        if damage == 'node':
            series['resource']['labels']['node'] = 'other'
        elif damage == 'duplicate':
            value['timeSeries'].append(series)
        elif damage == 'interval':
            series['points'][0]['interval']['endTime'] = '2026-10-12T00:00:02.000000002Z'
        elif damage == 'error':
            value['executionErrors'] = [dict(code=1)]
        else:
            series['points'][0]['value']['doubleValue'] = 2
        pages[1]['original'] = files.obj('changed', value)
    with pytest.raises(ValueError):
        parse_monitoring(files, pages)


@pytest.mark.parametrize('increment', [3, 24])
def test_cpu_increase_and_strict_headroom(tmp_path, increment):
    files = PackFiles(tmp_path)
    baseline = parse_monitoring(files, monitoring(files, 'base'))
    trial = parse_monitoring(files, monitoring(files, 'trial', (.22, .25)))
    with pytest.raises(ValueError, match='fleet_headroom_gap'):
        compare_cpu(baseline, trial, dict(numerator=increment, denominator=1))


def archive(tmp_path):
    path = tmp_path / 'download.zip'
    with zipfile.ZipFile(path, 'w') as output:
        output.writestr('exact/result.json', b'{"conclusion":"success"}')
        output.writestr('other/result.json', b'{"conclusion":"failure"}')
    raw = b'{"conclusion":"success"}'
    return path, dict(name='exact/result.json', bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())


def test_archive_member_digest_and_exact_name(tmp_path):
    path, member = archive(tmp_path)
    files = PackFiles(tmp_path / 'selected')
    selected = archive_members(files, path, archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                               members=[member], prefix='ci')
    assert files.read(selected[0]) == b'{"conclusion":"success"}'
    assert 'ci/selection.json' in files.refs
    member['sha256'] = '0' * 64
    with pytest.raises(ValueError, match='artifact_gap'):
        archive_members(PackFiles(tmp_path / 'wrong'), path,
            archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), members=[member], prefix='ci')


@pytest.mark.parametrize('name', ['result.json', 'other/result.json'])
def test_archive_cannot_choose_by_basename(tmp_path, name):
    path, member = archive(tmp_path)
    member['name'] = name
    with pytest.raises(ValueError, match='artifact_gap'):
        archive_members(PackFiles(tmp_path / 'wrong'), path,
            archive_sha256=hashlib.sha256(path.read_bytes()).hexdigest(), members=[member], prefix='ci')


def test_external_scope_derived_before_supplied_checks(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_external import REQUIRED_STAGES
    assert set(CHECK_SCOPE) == {name for names in REQUIRED_STAGES.values() for name in names}
    assert check_scope('policy_rollback_status', 'transport') != check_scope('policy_rollback_status', 'pre-flip')
    for checks in ([], ['made_up'], [*check_scope('health', 'transport'), 'extra']):
        with pytest.raises(ValueError, match='artifact_gap'):
            scoped_result(PackFiles(tmp_path), name='health', stage='transport', approved_checks=checks)


def test_publisher_configured_arithmetic():
    actual = publisher_projection(fleet_cap=10, surge=2, publishers=3)
    assert actual['selects'] == dict(numerator=3, denominator=1)
    assert actual['control_reads'] == dict(numerator=5, denominator=2)
    assert actual['transactions'] == dict(numerator=2, denominator=1)
    assert actual['unresolved_rows'] == dict(numerator=10001, denominator=2)
    assert actual['metadata_bytes'] == dict(numerator=204, denominator=1)
    assert actual['observer_reads'] == dict(numerator=15, denominator=1)
    assert actual['pending_rows'] == dict(numerator=3003, denominator=1)


def test_every_peak_requires_original_derivation(tmp_path):
    files = PackFiles(tmp_path)
    source = files.obj('measured', peaks())
    derivations = {name: dict(kind='measured', original=source, field=[name]) for name in peaks()}
    assert derived_peaks(files, derivations) == peaks()
    for name in peaks():
        changed = copy.deepcopy(derivations)
        del changed[name]
        with pytest.raises(ValueError):
            derived_peaks(files, changed)
    changed_values = peaks()
    changed_values['cpu_percent'] = None
    derivations['cpu_percent']['original'] = files.obj('null', changed_values)
    with pytest.raises(ValueError):
        derived_peaks(files, derivations)


def test_streaming_day_originals_peak_and_each_leaf_required(tmp_path):
    # 32 MiB day, 256 KiB per original. Accumulating the day fails the 4 MiB bound.
    source = PackFiles(tmp_path / 'source')
    refs = [source.put(f'day/{index:04d}', bytes([index % 256]) * 262144) for index in range(128)]
    out = PackFiles(tmp_path / 'pages')
    tracemalloc.start()
    try:
        with OriginalSpool(tmp_path / 'spool') as spool:
            assert spool.capture(source, iter(refs), producer='NativeExport') == 128
            leaves = spool.leaves(out)
            assert spool.validate_leaves(out, leaves) == 128
            _, peak = tracemalloc.get_traced_memory()
            assert peak < 4 * 1024 * 1024, peak
            for leaf in leaves:
                path = out.root / leaf['bundle']['path']
                raw = path.read_bytes()
                path.unlink()
                with pytest.raises(OSError):
                    spool.validate_leaves(out, leaves)
                path.write_bytes(raw)
            with pytest.raises(ValueError):
                spool.validate_leaves(out, leaves[:-1])
    finally:
        tracemalloc.stop()


def test_captured_transport_split_over_bundle_limit(tmp_path):
    source = tmp_path / 'captured.json'
    with source.open('w') as target:
        target.write('{"policy_version":"shadow-v2","contract_revision":"a1","days":["2026-10-12"],"preimages":[')
        for index in range(10001):
            if index:
                target.write(',')
            json.dump(dict(name=f'source/{index:05d}', raw='original'), target)
        target.write(']}')
    plan = split_transport(source, tmp_path / 'parts', max_objects=1000)
    assert plan['objects'] == 10001 and len(plan['parts']) == 11
    files = PackFiles(tmp_path / 'parts')
    assert len(check_plan(files, plan['inputs'])) == 12
    children = [child for ref in plan['parts'] for child in json.loads(files.read(ref))]
    assert [c['value']['name'] for c in children] == [f'source/{i:05d}' for i in range(10001)]
    for ref in plan['parts']:
        path = files.root / ref['path']
        raw = path.read_bytes()
        path.unlink()
        with pytest.raises(OSError):
            check_plan(files, plan['inputs'])
        path.write_bytes(raw)


@pytest.mark.parametrize('raw', ['{"rows":[],"rows":[]}', '{"rows":[1,]}', '{"rows":[],}', '{"rows":[]}x'])
def test_streaming_transport_rejects_ambiguous_or_partial_json(tmp_path, raw):
    import io

    from trusted_router.async_settle_shadow_v2_pack_streaming import CapturedJSON
    with pytest.raises(ValueError):
        list(CapturedJSON(io.StringIO(raw)).members())


def test_basis_index_expects_before_presence():
    from trusted_router.async_settle_shadow_v2_pack_references import BasisReferenceIndex
    reader = dict(day=DAY, boot='w1', health_slot=10, basis_sha256='a' * 64,
                  page='health-basis-batches/' + DAY + '/w1/00000000000000000001')
    index = BasisReferenceIndex.build(receipts=[], samples=[], readers=[reader], batches=[])
    assert index.keys == (reader['page'],)
    assert index.digests == ('a' * 64,)
    assert index.missing == ('basis:' + 'a' * 64, 'page:' + reader['page'])


def test_unreferenced_basis_page_is_gap():
    from trusted_router import async_settle_shadow_v2_a1_health as health
    from trusted_router.async_settle_shadow_v2_pack_references import BasisReferenceIndex
    from trusted_router.async_settle_shadow_v2_schema import sha256
    basis = dict(v=2, policy_version='shadow-v2', contract_revision='a1', reader_boot='w1',
                 source_state='empty', body=None, committed_at_ns=None, read_at_ns=None,
                 read_elapsed_ns=1, since_reply_ns=0)
    page = dict(v=2, policy_version='shadow-v2', contract_revision='a1', boot='w1', day=DAY,
                sequence='00000000000000000001', previous_sha256=None, bases=[basis])
    health.HEALTH_BASIS_BATCH(page)
    key = 'health-basis-batches/' + DAY + '/w1/00000000000000000001'
    index = BasisReferenceIndex.build(receipts=[], samples=[], readers=[], batches=[(key, page)])
    assert index.missing == tuple(sorted(('unreferenced_basis:' + sha256(basis), 'unreferenced_page:' + key)))
    reader = dict(day=DAY, boot='w1', health_slot=None, basis_sha256=sha256(basis), page=key)
    index = BasisReferenceIndex.build(receipts=[], samples=[], readers=[reader], batches=[(key, page)])
    assert not index.missing and index.keys == (key,)


def test_current_loader_cap_result_is_expected_base_defect():
    from trusted_router import async_settle_shadow_v2_a1_evidence as p1
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    cap = dict(v=2, policy_version='shadow-v2', contract_revision='a1', day=DAY, terminal_reserved=0, event_reserved=0)
    raw = canonical(dict(policy_version='shadow-v2', contract_revision='a1', days=[DAY], completed_at_us=START,
        rows=[dict(kind=p1.KINDS[5], id=DAY + '/cap-v2', raw=p1.CAP.serialize(cap).decode())]))
    loaded = load(raw)
    assert loaded.proof is None
    assert loaded.a2_streams['loading_issues'] == [
        {'gap': 'artifact_gap', 'path': 'proof.shutdown_inventory_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'proof.cohort_seed_policy_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'proof.pre_budget_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'proof.post_budget_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'proof.external_proofs_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'proof.resolution_dag_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'manifest.independent_inventory_sha256', 'detail': 'artifact_gap'},
        {'gap': 'artifact_gap', 'path': 'loading/release-plan.json', 'detail': 'artifact_gap'},
        {'gap': 'artifact_gap', 'path': 'loading/signatures.json', 'detail': 'artifact_gap'},
    ], 'A3-E base changed; update this expectation deliberately'

    # Expected after the A3-E correction; current output above remains asserted.
    from tests.shadow_v2_pack_round5_vectors import corrected_result
    corrected = corrected_result('test_current_loader_cap_result_is_expected_base_defect')
    assert corrected['rows'] == 1 and len(corrected['loading_validation']) == 25


def test_stage_registers_every_producer_source_fixture_and_review(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_stage import (
        PRODUCERS,
        StageAssemblyInputs,
        StreamingStageAssembly,
    )
    source = PackFiles(tmp_path / 'source')
    original = source.put('source-original', b'original')
    fixture = source.put('fixture-original', b'fixture')
    producers = {name: [source.obj('producer/' + name, dict(day=DAY, source=original, fixture=fixture))]
                 for name in PRODUCERS}
    inputs = StageAssemblyInputs.parse(dict(days=[DAY], producers=producers, sources=[original],
                                          fixtures=[fixture], predecessors=[]))
    found = StreamingStageAssembly.capture(source, tmp_path / 'stage', inputs)
    assert found.objects == len(PRODUCERS) + 2
    status = json.loads((tmp_path / 'stage/stage-inputs.json').read_bytes())
    names = [r['path'] for entry in status['bundles']
             for r in json.loads((tmp_path / 'stage' / entry['bundle']['path']).read_bytes())['keys']]
    assert set(names) == set(source.refs)
    (source.root / original['path']).unlink()
    with pytest.raises(OSError):
        StreamingStageAssembly.capture(source, tmp_path / 'missing', inputs)
    for producer in PRODUCERS:
        changed = copy.deepcopy(producers)
        changed[producer] = []
        with pytest.raises(ValueError, match='StageAssemblyInputs'):
            StageAssemblyInputs.parse(dict(days=[DAY], producers=changed, sources=[original], fixtures=[fixture], predecessors=[]))


def test_stage_missing_source_registration_blocks(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_stage import (
        PRODUCERS,
        StageAssemblyInputs,
        StreamingStageAssembly,
    )
    source = PackFiles(tmp_path / 'source')
    hidden = source.put('unlisted', b'original')
    listed = source.put('listed', b'listed')
    producers = {name: [source.obj('producer/' + name, dict(source=hidden))] for name in PRODUCERS}
    inputs = StageAssemblyInputs.parse(dict(days=[DAY], producers=producers,
        sources=[listed], fixtures=[listed], predecessors=[]))
    with pytest.raises(ValueError, match='ReleaseReviewAssembly'):
        StreamingStageAssembly.capture(source, tmp_path / 'stage', inputs)


def test_frozen_validation_capacity_is_named_before_reads(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_stage import validate_capacity
    with pytest.raises(ValueError, match='FrozenDayValidationCapacity'):
        validate_capacity(PackFiles(tmp_path), [dict(path='not-opened', sha256='a' * 64, bytes=100)], bound=99)


def test_window_exact_offline_commands_and_action_seams(tmp_path, capsys):
    from scripts.async_settle.shadow_v2_window import main
    plan = tmp_path / 'plan.json'
    plan.write_text('{}')
    for action, phase, seam in [('trial', None, 'P6NativeQualification'),
        ('release', 'shadow-opt-in', 'P6ReviewedRelease'), ('probe', 'warmup', 'P6DedicatedJob')]:
        args = [action, '--plan', str(plan)]
        if phase:
            args += ['--phase', phase]
        else:
            args += ['--out', str(tmp_path / 'trial')]
        assert main(args) == 1
        assert seam in capsys.readouterr().out
    assert not (tmp_path / 'trial').exists()
    source = PackFiles(tmp_path / 'source')
    ref = source.put('one', b'original')
    plan.write_text(json.dumps(dict(files=[ref], subjects={'one': ref}, criteria={}, required_stages={}, verifiers={})))
    assert main(['review-inputs', '--plan', str(plan), '--input', str(source.root)]) == 1
    assert (source.root / 'review-inputs/review/subjects.json').exists()
    before = (source.root / 'review-inputs/review/subjects.json').read_bytes()
    assert main(['review-inputs', '--plan', str(plan), '--input', str(source.root)]) == 1
    assert (source.root / 'review-inputs/review/subjects.json').read_bytes() == before
    assert main(['pack', '--plan', str(plan), '--input', str(source.root), '--out', str(tmp_path / 'packed')]) == 1
    assert 'StageAssemblyInputs' in capsys.readouterr().out


async def test_terminal_backing_real_writer_and_cross_day_support(monkeypatch, tmp_path):
    from tests import test_async_settle_shadow_v2_pack_integration as integration
    from trusted_router import async_settle_shadow_v2_a1_reconciliation as joins
    from trusted_router import async_settle_shadow_v2_evidence as p1
    from trusted_router.async_settle_shadow_v2_pack_references import TerminalReconciliation
    original_load = integration.load
    inspected = []

    def checked_load(raw):
        bundle = json.loads(raw)
        pages = [p1.PAGE.parse(row['raw'].encode()) for row in bundle['rows'] if row['kind'] == p1.KINDS[2]]
        samples = [p1.SAMPLE.parse(row['raw'].encode()) for row in bundle['rows'] if row['kind'] == p1.KINDS[0]]
        reconciliation = TerminalReconciliation(pages, samples)
        issuances = [(page, event) for page in pages for event in page['events'] if event['phase'] == 'authorize']
        assert len(issuances) == 3
        for page, event in issuances:
            entry = joins.authorization_obligation(page, event['seq'])
            assert reconciliation.terminal(entry) is not None
            with pytest.raises(ValueError, match='eligible_coverage_gap'):
                TerminalReconciliation(pages, []).terminal(entry)
        selected = ['2026-10-06', '2026-10-07', '2026-10-08', '2026-10-09', '2026-10-10', '2026-10-11', DAY]
        assert reconciliation.support_days(selected)[-1] == '2026-10-14'
        page, event = issuances[0]
        entry = joins.authorization_obligation(page, event['seq'])
        terminal_page, loc = reconciliation.terminal(entry)
        terminal = next(e for e in terminal_page['events'] if e['seq'] == loc['event_seq'])
        end = copy.deepcopy(terminal)
        end['event_at_us'] += 86400000000
        end['receipt_at_us'] += 86400000000
        later = dict(v=2, policy_version='shadow-v2', day='2026-10-13', boot=terminal_page['boot'],
                     first_seq=end['seq'], last_seq=end['seq'], previous_page_sha256=None, events=[end])
        issue_page = copy.deepcopy(page)
        issue_page['events'] = [copy.deepcopy(event)]
        issue_page['first_seq'] = issue_page['last_seq'] = event['seq']
        issue_page['events'][0]['receipt_locator']['first_seq'] = event['seq']
        sample = copy.deepcopy(next(s for s in samples if s['original_terminal_receipt']['event_seq'] == end['seq']))
        sample['original_terminal_receipt']['day'] = later['day']
        cross = TerminalReconciliation([issue_page, later], [sample])
        assert cross.terminal(joins.authorization_obligation(issue_page, event['seq']))[0]['day'] == '2026-10-13'
        replay = copy.deepcopy(end)
        replay.update(phase='replay', event_at_us=event['event_at_us'], receipt_at_us=event['receipt_at_us'],
            prediction=None, completion_of=None, retry_original=dict(day='2026-10-11', boot='missing', event_seq='00000000000000000001'))
        replay_page = dict(v=2, policy_version='shadow-v2', day=DAY, boot=page['boot'],
            first_seq=replay['seq'], last_seq=replay['seq'], previous_page_sha256=None, events=[replay])
        with pytest.raises(ValueError, match='authorization_link_gap'):
            TerminalReconciliation([replay_page], []).support_days([DAY])
        inspected.append(True)
        return original_load(raw)

    monkeypatch.setattr(integration, 'load', checked_load)
    await integration.test_real_writers_native_export_collect_and_pack_loader(monkeypatch, tmp_path)
    assert inspected == [True]


def test_transport_large_original_is_split_and_restored(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_streaming import restore_transport
    value = dict(policy_version='shadow-v2', contract_revision='a1', days=[DAY],
                 preimages=[dict(name='large-original', raw='x' * 262144)])
    source = tmp_path / 'captured.json'
    source.write_bytes(canonical(value))
    plan = split_transport(source, tmp_path / 'parts')
    files = PackFiles(tmp_path / 'parts')
    assert len(check_plan(files, plan['inputs'])) > 2
    restored = json.loads(restore_transport(files, plan, tmp_path / 'restored').read_bytes())
    assert restored['preimages'] == value['preimages']
    child = next(ref for ref in check_plan(files, plan['inputs']) if '/children/' in ref['path'])
    (files.root / child['path']).unlink()
    with pytest.raises(OSError):
        restore_transport(files, plan, tmp_path / 'missing')


def test_window_pack_large_transport_uses_plan(tmp_path):
    from scripts.async_settle.shadow_v2_window import window
    path = tmp_path / 'captured.json'
    path.write_bytes(canonical(dict(policy_version='shadow-v2', contract_revision='a1', days=[DAY],
        preimages=[dict(name=f'original/{i}', raw='x' * 10000) for i in range(500)])))
    result = window('pack', dict(captured_transport='captured.json'), source=tmp_path, out=tmp_path / 'out')
    assert result['input_objects'] == 500
    assert result['missing'] == ['MissingLaterFilePlan', 'MultiBundleLoading']
    plan = json.loads((tmp_path / 'out/transport-plan.json').read_bytes())
    assert len(plan['parts']) > 1
    assert check_plan(PackFiles(tmp_path / 'out'), plan['inputs'])


def test_pre_post_allowances_must_match_exactly(tmp_path):
    from trusted_router.async_settle_shadow_v2_gate_inputs import ALLOWANCES, COMPONENTS, TOTALS
    from trusted_router.async_settle_shadow_v2_pack_sources import equal_allowances
    files = PackFiles(tmp_path)
    original = {**{name: dict(numerator=1, denominator=1) for name in ALLOWANCES},
                **dict.fromkeys(TOTALS, 1), 'component_us': dict.fromkeys(COMPONENTS, 1)}
    pre = files.obj('pre', original)
    post = files.obj('post', original)
    equal_allowances(files, pre, post)
    for name in (*ALLOWANCES, *TOTALS):
        changed = copy.deepcopy(original)
        changed[name] = dict(numerator=2, denominator=1) if name in ALLOWANCES else 2
        ref = files.obj('changed/' + name, changed)
        with pytest.raises(ValueError, match='artifact_gap'):
            equal_allowances(files, pre, ref)


def test_external_supplied_scope_cannot_shorten_design(tmp_path):
    from tests.test_async_settle_shadow_v2_pack import REV
    names = list(check_scope('health', 'transport'))
    for label, supplied in [('full', names), ('short', names[:-1])]:
        files = PackFiles(tmp_path / label)
        source = files.obj('ci-original', dict(explicit_fake=True, checks=supplied))
        kwargs = dict(name='health', stage='transport', router_revision=REV, enclave_revision='b' * 40,
            completed_at_us=START, producer='fake-ci', approved_checks=supplied,
            checks=[dict(name=name, result='pass', evidence=source) for name in supplied], sources=[source])
        if label == 'full':
            assert len(scoped_result(files, **kwargs)['checks']) == len(names)
        else:
            with pytest.raises(ValueError, match='artifact_gap'):
                scoped_result(files, **kwargs)


def test_cap_survives_split_transport_with_expected_base_result(tmp_path):
    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_pack_streaming import restore_transport
    cap = dict(v=2, policy_version='shadow-v2', contract_revision='a1', day=DAY,
               terminal_reserved=0, event_reserved=0)
    captured = tmp_path / 'original.json'
    captured.write_bytes(canonical(dict(policy_version='shadow-v2', contract_revision='a1', days=[DAY],
        completed_at_us=START, rows=[dict(kind=a1.KINDS[5], id=DAY + '/cap-v2', raw=a1.CAP.serialize(cap).decode())])))
    plan = split_transport(captured, tmp_path / 'parts')
    restored = restore_transport(PackFiles(tmp_path / 'parts'), plan, tmp_path / 'restored')
    loaded = load(restored.read_bytes())
    assert loaded.a2_streams['loading_issues'] == [
        {'gap': 'artifact_gap', 'path': 'proof.shutdown_inventory_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'proof.cohort_seed_policy_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'proof.pre_budget_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'proof.post_budget_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'proof.external_proofs_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'proof.resolution_dag_sha256', 'detail': 'required payload absent'},
        {'gap': 'artifact_gap', 'path': 'manifest.independent_inventory_sha256', 'detail': 'artifact_gap'},
        {'gap': 'artifact_gap', 'path': 'loading/release-plan.json', 'detail': 'artifact_gap'},
        {'gap': 'artifact_gap', 'path': 'loading/signatures.json', 'detail': 'artifact_gap'},
    ]

    # Expected after the A3-E correction; current output above remains asserted.
    from tests.shadow_v2_pack_round5_vectors import corrected_result
    corrected = corrected_result('test_cap_survives_split_transport_with_expected_base_result')
    assert corrected['rows'] == 1 and len(corrected['loading_validation']) == 25


def test_unnamed_basis_inside_named_page_is_gap():
    from trusted_router.async_settle_shadow_v2_pack_references import BasisReferenceIndex
    from trusted_router.async_settle_shadow_v2_schema import sha256
    basis = dict(v=2, policy_version='shadow-v2', contract_revision='a1', reader_boot='w1',
        source_state='empty', body=None, committed_at_ns=None, read_at_ns=None, read_elapsed_ns=1, since_reply_ns=0)
    unnamed = {**basis, 'read_elapsed_ns': 2}
    key = 'health-basis-batches/' + DAY + '/w1/00000000000000000001'
    page = dict(v=2, policy_version='shadow-v2', contract_revision='a1', boot='w1', day=DAY,
        sequence='00000000000000000001', previous_sha256=None, bases=sorted([basis, unnamed], key=sha256))
    reader = dict(day=DAY, boot='w1', health_slot=None, basis_sha256=sha256(basis), page=key)
    index = BasisReferenceIndex.build(receipts=[], samples=[], readers=[reader], batches=[(key, page)])
    assert index.missing == ('unreferenced_basis:' + sha256(unnamed),)
