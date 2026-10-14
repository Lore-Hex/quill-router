"""Finite captured files and unchanged contract/loader boundaries."""
from __future__ import annotations

import copy
import json
from collections import Counter
from fractions import Fraction

import pytest

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_collect_ports import TAGS
from trusted_router.async_settle_shadow_v2_gate_loader import load
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_pack_budget import (
    COMPONENTS,
    cpu_peak,
    measured_components,
    nominal,
    segments,
    shutdown,
)
from trusted_router.async_settle_shadow_v2_pack_coverage import ExactPages, chain, coverage, handoff
from trusted_router.async_settle_shadow_v2_pack_daypack import bind_accounting, select_manifests
from trusted_router.async_settle_shadow_v2_pack_external import (
    REQUIRED_STAGES,
    ci_originals,
    policy,
    proof_result,
)
from trusted_router.async_settle_shadow_v2_pack_history import history
from trusted_router.async_settle_shadow_v2_pack_plan import (
    PackFiles,
    check_plan,
    input_plan,
    review_subjects,
    seal_review,
)
from trusted_router.async_settle_shadow_v2_schema import Schema, integer, nullable, sha256
from trusted_router.detached_jws import canonical

DAY = '2026-10-12'
START = 1791763200000000
BOOT = '11111111-1111-4111-8111-111111111111'
REV = 'a' * 40


def review(files, subject, path='review.json'):
    value = dict(**TAGS, subject_sha256=subject['sha256'], reviewer='fake-reviewer',
                 reviewed_at_us=START, decision='approved')
    ref = files.obj(path, value, a2.REVIEW_RECORD)
    return {k: v for k, v in value.items() if k not in TAGS} | {'record': ref}


def peaks():
    return {key: 1 if key in ('simultaneous_routers', 'simultaneous_publishers', 'configured_routers',
             'configured_publishers', 'startup_operations_1s', 'close_operations_1s', 'export_operations_1s')
            else dict(numerator=1, denominator=1) for key in a2.PEAKS.fields}


def test_multi_bundle_more_than_ten_thousand_and_each_bundle_required(tmp_path):
    # Finite captured-file port: original bytes are in memory, while the actual
    # bounded leaves, inventory pages and plan are written to the local spool.
    class CapturedFiles(PackFiles):
        captured = {}

        def put(self, path, raw, schema=None):
            if not path.startswith('original/'):
                return super().put(path, raw, schema)
            from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
            ref = dict(path=path, sha256=file_digest(raw), bytes=len(raw))
            a2.FILE_REF(ref)
            self.captured[path] = raw
            self.refs[path] = ref
            return ref

        def read(self, ref):
            if ref['path'] not in self.captured:
                return super().read(ref)
            from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
            raw = self.captured[ref['path']]
            assert len(raw) == ref['bytes'] and file_digest(raw) == ref['sha256']
            return raw

    files = CapturedFiles(tmp_path)
    originals = [files.put(f'original/{i:05d}', canonical(dict(original=i))) for i in range(10001)]
    plan = input_plan(files, originals, days=[DAY], predecessors=[originals[0]])
    assert len(plan['bundles']) > 1
    assert len(check_plan(files, plan)) == 10001
    graph = PayloadGraph(files.originals())
    assert graph.detail(plan['inventory'], 'input_inventory') == plan['bundles']
    for entry in plan['bundles']:
        path = tmp_path / entry['bundle']['path']
        raw = path.read_bytes()
        path.unlink()
        with pytest.raises((ValueError, OSError)):
            check_plan(files, plan)
        path.write_bytes(raw)
    removed = copy.deepcopy(plan)
    removed['bundles'].pop()
    with pytest.raises(ValueError):
        check_plan(files, removed)
    # The current loader still enforces a combined ceiling; preserve its result.
    raw = canonical(dict(policy_version='shadow-v2', contract_revision='a1', days=[DAY], completed_at_us=START,
        preimages=[dict(name=ref['path'], raw=files.read(ref).decode()) for ref in originals]))
    result = load(raw)
    assert result.proof is None
    assert any(i['gap'] == 'artifact_gap' for i in result.a2_streams['loading_issues'])


def test_plan_bytes_deterministic_and_dependency_deletion(tmp_path):
    outputs = []
    for name in ('a', 'b'):
        files = PackFiles(tmp_path / name)
        refs = [files.put('source/b', b' b\n'), files.put('source/a', b'a')]
        plan = input_plan(files, refs if name == 'a' else refs[::-1], days=[DAY], predecessors=[])
        outputs.append(files.originals())
        (files.root / 'source/a').unlink()
        with pytest.raises(OSError):
            check_plan(files, plan)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize('change', ['bytes', 'sha256', 'path', 'objects', 'first_key', 'last_key'])
def test_plan_original_changes_block(tmp_path, change):
    files = PackFiles(tmp_path)
    ref = files.put('source/a', b'a')
    plan = input_plan(files, [ref], days=[DAY], predecessors=[])
    if change in ('objects', 'first_key', 'last_key'):
        plan['bundles'][0][change] = 2 if change == 'objects' else 'wrong'
    else:
        plan['bundles'][0]['bundle'][change] = 1 if change == 'bytes' else '0' * 64 if change == 'sha256' else 'absent'
    with pytest.raises((ValueError, OSError)):
        check_plan(files, plan)


def test_review_never_creates_human_record_and_seal_checks_original(tmp_path):
    files = PackFiles(tmp_path)
    ref = files.put('source/a', b'a')
    subjects = review_subjects(files, {'input': ref}, criteria={}, required_stages={}, verifiers={})
    assert not any(path.endswith('review.json') for path in files.refs)
    with pytest.raises(ValueError):
        seal_review(files, subjects, approvals={}, release_sha256=subjects['release_plan']['sha256'])
    supplied = review(files, ref)
    released = json.loads(files.read(subjects['release_plan']))
    released['artifacts'][supplied['record']['path']] = supplied['record']['sha256']
    subjects['release_plan'] = files.obj('review/final-release-plan.json', released)
    result = seal_review(files, subjects, approvals={'input': supplied},
                         release_sha256=subjects['release_plan']['sha256'])
    assert result['state'] == 'reviewed'
    changed = {**supplied, 'reviewer': 'different'}
    with pytest.raises(ValueError):
        seal_review(files, subjects, approvals={'input': changed}, release_sha256=subjects['release_plan']['sha256'])


@pytest.mark.parametrize('stream', a1.COVERAGE_STREAMS)
def test_every_coverage_population_missing_and_conflict_retained(tmp_path, stream):
    calls = Counter()
    def get(key):
        assert source.expected == {name + '/' + key for name in a1.COVERAGE_STREAMS
                                   for key in ('missing', 'conflict')}
        calls[key] += 1
        return (b'one', b'two') if key.endswith('/conflict') else ()
    source = ExactPages(get)
    expected = {name: ['missing', 'conflict'] for name in a1.COVERAGE_STREAMS}
    files = PackFiles(tmp_path)
    result = coverage(files, source, day=DAY, expected=expected)
    entries = result[stream][0]['entries']
    assert [(e['key'], e['state']) for e in entries] == [('conflict', 'conflict'), ('missing', 'missing')]
    assert len([p for p in files.refs if p.startswith('candidates/')]) == 2
    coverage(files, source, day=DAY, expected=expected)
    assert calls == Counter({name + '/' + key: 1 for name in a1.COVERAGE_STREAMS
                             for key in ('missing', 'conflict')})
    with pytest.raises(ValueError):
        source.read('not-planned')


def test_missing_predecessor_keeps_whole_tail():
    schema = Schema(dict(first=integer, last=integer, previous=nullable(a2.digest)), 1024)
    first = dict(first=1, last=2, previous=None)
    originals = {'1': (canonical(first),)}
    source = ExactPages(lambda key: originals.get(key, ()))
    result = chain(source, first=1, last=12, key=str, schema=schema,
                   first_field='first', last_field='last', previous_field='previous')
    assert result.keys == ('1', '3')
    assert result.tail.first == 3 and result.tail.last == 12
    assert result.tail.reason == 'missing_or_conflicting_predecessor'


def test_handoff_missing_is_not_zero_expected():
    value = handoff(DAY, BOOT, None)
    assert value['expected_handoffs'] == value['attempts_upper'] == 1
    assert value['attempts_lower'] == 0 and value['result_sha256'] is None


def test_n_one_nominal_and_measured_max():
    assert nominal(1, 0, 0, 4000000) == dict(reads_per_second=dict(numerator=5, denominator=4),
                                           pending_rows_per_second=dict(numerator=1001, denominator=4))
    assert nominal(1, 8, 16, 4000000)['reads_per_second'] == dict(numerator=6, denominator=1)


@pytest.mark.parametrize('name', COMPONENTS)
def test_each_component_missing_or_over_bound_blocks(name):
    data = {k: [0] for k in COMPONENTS}
    del data[name]
    with pytest.raises(ValueError):
        measured_components(data)
    data[name] = [100000000]
    with pytest.raises(ValueError):
        measured_components(data)


def monitoring():
    return dict(source='monitoring.timeSeries.list', node='actual-node', sample_interval_us=1000000,
        complete=True, samples=[dict(start_us=i * 1000000, end_us=(i + 1) * 1000000,
                                    percent=dict(numerator=30, denominator=1)) for i in range(3)])


@pytest.mark.parametrize('damage', ['sample', 'node', 'interval', 'ceiling', 'complete'])
def test_cpu_missing_samples_and_limit_block(damage):
    data = monitoring()
    increment = dict(numerator=10, denominator=1)
    assert cpu_peak(data, node='actual-node', start_us=0, end_us=3000000,
                    sample_interval_us=1000000, approved_increment=increment) == Fraction(30)
    if damage == 'sample':
        del data['samples'][1]
    elif damage == 'node':
        data['node'] = 'different-node'
    elif damage == 'interval':
        data['sample_interval_us'] = 2000000
    elif damage == 'complete':
        data['complete'] = False
    else:
        increment['numerator'] = 15
    with pytest.raises(ValueError):
        cpu_peak(data, node='actual-node', start_us=0, end_us=3000000,
                 sample_interval_us=1000000, approved_increment=increment)


def shutdown_data():
    original = dict(day=DAY, boot=BOOT, complete=True, outcome='graceful', spans={
        'preparation': [0, 2000000], 'export_wait': [2000000, 5000000],
        'final_pass': [5000000, 9000000], 'close': [5000000, 7000000], 'result': [7000000, 9000000]})
    measurement = dict(**TAGS, day=DAY, boot=BOOT, grace_us=9000000, preparation_us=2000000,
        export_wait_us=3000000, final_pass_us=4000000, close_us=2000000, result_us=2000000,
        outcome='graceful', source=dict(path='span.json', sha256=sha256(original), bytes=len(canonical(original))))
    return measurement, original


def test_shutdown_nested_spans_once_and_false_zero_blocks():
    measurement, original = shutdown_data()
    assert shutdown(measurement, original) == 9000000
    measurement['result_us'] = 0
    with pytest.raises(ValueError):
        shutdown(measurement, original)


@pytest.mark.parametrize('span', ['preparation', 'export_wait', 'final_pass', 'close', 'result'])
def test_each_shutdown_span_missing_blocks(span):
    measurement, original = shutdown_data()
    del original['spans'][span]
    with pytest.raises(ValueError):
        shutdown(measurement, original)


def test_segment_midnight_rate_role_and_missing_peak():
    start = START + 86400000000 - 1000000
    item = dict(day=DAY, boot=BOOT, region='region', start_us=start, end_us=start + 2000000,
                role='router', contender=True, workspace_count=1, peaks=peaks())
    found = segments([item])
    assert len(found) == 2 and found[0]['end_us'] == found[1]['start_us']
    assert [r['day'] for r in found] == [DAY, '2026-10-13']
    for field in a2.PEAKS.fields:
        changed = copy.deepcopy(item)
        changed['peaks'][field] = None
        with pytest.raises(ValueError):
            segments([changed])


def test_control_subtypes_are_preserved():
    raw = a1.CAP.serialize(dict(v=2, policy_version='shadow-v2', contract_revision='a1', day=DAY,
                               terminal_reserved=0, event_reserved=0))
    rows = [dict(kind=a1.KINDS[5], id=DAY + '/cap-v2', raw=raw.decode())]
    before = copy.deepcopy(rows)
    assert select_manifests(rows) == []
    assert rows == before


def test_accounting_binding_preserves_serving(tmp_path):
    files = PackFiles(tmp_path)
    ref = files.obj('export/accounting.json', dict(format='shadow-v2-live-export-a3-n', day=DAY))
    serving = [dict(instance='fake-id')]
    deployment = dict(serving=serving)
    result = bind_accounting(files, deployment, day=DAY, accounting=[ref])
    assert result['serving'] is serving and 'export_accounting' not in deployment
    assert PayloadGraph(files.originals()).detail(result['export_accounting'], 'files') == [ref]


@pytest.mark.parametrize('stage', REQUIRED_STAGES)
def test_whole_external_policy_cannot_move_or_delete_names(stage):
    value = {s: list(names) for s, names in REQUIRED_STAGES.items()}
    policy(value)
    value[stage].pop()
    with pytest.raises(ValueError):
        policy(value)


def test_ci_original_conclusion_and_completed_time(tmp_path):
    files = PackFiles(tmp_path)
    values = dict(run=dict(id=1, head_sha=REV, status='completed', conclusion='success', updated_at='2026-10-12T00:00:02Z'),
        job=dict(id=2, run_id=1, head_sha=REV, status='completed', conclusion='success', completed_at='2026-10-12T00:00:01Z'),
        artifact=dict(id=3, workflow_run=dict(id=1, head_sha=REV), expired=False),
        result=dict(revision=REV, run_id=1, job_id=2, artifact_id=3, conclusion='success', completed_at='2026-10-12T00:00:01Z'))
    refs = {key: files.obj(key, value) for key, value in values.items()}
    assert ci_originals(files, **refs, revision=REV) == START + 1000000
    changed = {**values['job'], 'name': 'success', 'conclusion': 'failure'}
    refs['job'] = files.obj('job-failed', changed)
    with pytest.raises(ValueError):
        ci_originals(files, **refs, revision=REV)


def test_external_checks_and_sources_original_bytes(tmp_path):
    files = PackFiles(tmp_path)
    ref = files.obj('fake-ci-result', dict(conclusion='success'))
    value = proof_result(files, name='a1_contract', router_revision=REV, enclave_revision='b' * 40,
        completed_at_us=START, producer='fake-ci', approved_checks=['original_result'],
        checks=[dict(name='original_result', result='pass', evidence=ref)], sources=[ref])
    a2.PROOF_RESULT(value)
    assert PayloadGraph(files.originals()).detail(value['sources'], 'files') == [ref]
    with pytest.raises(ValueError):
        proof_result(files, name='a1_contract', router_revision=REV, enclave_revision='b' * 40,
            completed_at_us=START, producer='fake-ci', approved_checks=['original_result', 'missing'],
            checks=[dict(name='original_result', result='pass', evidence=ref)], sources=[ref])


def test_history_runs_both_retained_calls_and_preserves_blocked(tmp_path):
    files = PackFiles(tmp_path)
    payload = files.obj('old/payload', dict(rows=[], days=[DAY], proof={}, resets=[], serving=[], manifests={}))
    reviewed = files.obj('old/reviewed', dict(rows=[], resets=[], resolutions=[]))
    from pathlib import Path
    source = files.put('old/source', Path('scripts/async_settle/shadow_report.py').read_bytes())
    supplied_review = review(files, reviewed, 'old/review')
    value = history(files, payload=payload, source=source, dependencies=[], reviewed_history=reviewed, approvals=[supplied_review])
    a2.RESOLUTION_DAG(value)
    output = json.loads((tmp_path / 'history/output.json').read_bytes())
    assert output['report']['status'] == 'BLOCKED'
    graph = PayloadGraph(files.originals())
    graph.materialize({'proof.resolution_dag_sha256': value})
    assert not graph.issues
    assert value['history']['count'] == 0
    changed = files.obj('old/changed-review', dict(rows=[], resets=[{'revision': REV}], resolutions=[]))
    different = PackFiles(tmp_path / 'different')
    different.capture(files, [payload, source, changed])
    changed_review = review(different, changed, 'old/changed-review-record')
    with pytest.raises(ValueError):
        history(different, payload=payload, source=source, dependencies=[], reviewed_history=changed, approvals=[changed_review])


def test_export_rpc_and_rerun_ceiling_are_separate(tmp_path):
    from trusted_router.async_settle_shadow_v2_pack_budget import export_costs
    from trusted_router.async_settle_shadow_v2_pack_daypack import AccountingResult
    accounting = AccountingResult(DAY, (), (), native_rpcs=8, body_bytes=100, operations=4)
    approved = dict(native_rpcs=8, export_passes=1, export_operations=4, export_bytes=100, export_write_bytes=0)
    assert export_costs([accounting], [], approved=approved)['native_rpcs'] == 8
    with pytest.raises(ValueError):
        export_costs([accounting, accounting], [], approved=approved)
    changed = AccountingResult(DAY, (), (), native_rpcs=9, body_bytes=100, operations=4)
    with pytest.raises(ValueError):
        export_costs([changed], [], approved=approved)


def test_reviewed_caps_not_replaced_with_observed_demand(tmp_path):
    from trusted_router.async_settle_shadow_v2_gate_inputs import ALLOWANCES, TOTALS
    from trusted_router.async_settle_shadow_v2_pack_budget import budget_measurements
    files = PackFiles(tmp_path)
    ceilings = {key: dict(numerator=99, denominator=1) for key in ALLOWANCES} | dict.fromkeys(TOTALS, 99)
    reviewed = files.obj('reviewed/budget', ceilings)
    components = files.obj('measured/components', {name: [1] for name in COMPONENTS})
    measured = files.obj('measured/counts', dict(reads=1))
    result = budget_measurements(files, reviewed=reviewed, component_source=components, measured_sources=[measured])
    assert all(result[name] == ceilings[name] for name in ceilings)
    assert result['component_us'] == dict.fromkeys(COMPONENTS, 1)
    (tmp_path / measured['path']).unlink()
    with pytest.raises(OSError):
        budget_measurements(files, reviewed=reviewed, component_source=components, measured_sources=[measured])


def test_external_exact_separate_names():
    from trusted_router.async_settle_shadow_v2_gate_views import EXTERNAL_REQUIRED
    names = {name for values in REQUIRED_STAGES.values() for name in values}
    assert names == set(EXTERNAL_REQUIRED) | {'attestation', 'exposure_acceptance'}
    assert 'authorization_transport_measurements' in names and 'maximum_header_hops' in names


def test_independent_idle_and_terminated_writers_remain_expected():
    from trusted_router.async_settle_shadow_v2_pack_coverage import populations
    boots = tuple(dict(boot=name, role='router', start_us=START, end_us=START + 100)
                  for name in ('idle', 'terminated'))
    source = ExactPages(lambda key: ())
    result = populations(source, day=DAY, boots=boots, contender_segments=[], basis_keys=[], obligation_keys=[])
    assert result.expected['close_result'] == [DAY + '/idle', DAY + '/terminated']
    assert len(result.missing) == 2


def test_inventory_page_deletion_blocks_plan(tmp_path):
    files = PackFiles(tmp_path)
    plan = input_plan(files, [files.put('original', b'one')], days=[DAY], predecessors=[])
    (tmp_path / plan['inventory_pages'][0]['path']).unlink()
    with pytest.raises(OSError):
        check_plan(files, plan)
