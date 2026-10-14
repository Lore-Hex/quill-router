"""Captured operations through both retained budget envelopes."""
from __future__ import annotations

import copy
import json
from fractions import Fraction
from pathlib import Path

import pytest

from scripts.async_settle.async_settle_shadow_v2_gate_tools import run_retained
from tests.shadow_v2_pack_round3_captures import writer_capture
from tests.test_async_settle_shadow_v2_pack import (
    BOOT,
    DAY,
    REV,
    peaks,
    review,
    shutdown_data,
)
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_gate_inputs import ALLOWANCES, COMPONENTS, TOTALS
from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
from trusted_router.async_settle_shadow_v2_pack_budget import fraction
from trusted_router.async_settle_shadow_v2_pack_captured_budget import CapturedBudget
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import sha256


def budget_capture(files, capture):
    counter = next(json.loads(row['raw']) for row in capture['bundle']['rows'] if 'counter' in row['kind'])
    start, end = counter['started_at_us'], counter['flushed_at_us']
    counter_ref = files.obj('trial/counter', counter)
    trace_refs = []
    operations = []
    for key, ref in capture['objects'].refs.items():
        if not key.startswith('publisher-traces/'):
            continue
        raw = capture['objects'].read(ref)
        trace_refs.append(files.put('trial/' + key, raw))
        for entry in json.loads(raw)['entries']:
            for index, name in enumerate(('claims', 'scans', 'publications')):
                operations.append(dict(id=str(entry['slot']) + '-' + name, at_us=entry['slot'] * 2000000,
                    operation=name, logical_count=1, native_rpcs=entry['rpc_count'] if index == 0 else 0,
                    rows=1, bytes=24, elapsed_us=1, slot=entry['slot']))

    config = files.obj('trial/configuration', dict(workspace_count=1, fleet_cap=4,
        regional_caps={'us-central1': 4}, surge=1, publishers=2))
    from datetime import UTC, datetime
    resource = dict(type='spanner_instance', labels=dict(node='one'))
    for name, at in (('startup', start), ('close', end - 1), ('export', end - 1)):
        operations.append(dict(id=name, at_us=at, operation=name, logical_count=1,
            native_rpcs=1, rows=0, bytes=0, elapsed_us=1, slot=None))
    native = files.obj('trial/native-operations', dict(day=DAY, boot=BOOT, start_us=start,
        end_us=end, complete=True, operations=operations, configuration=config,
        resource=resource, region='us-central1'))
    def series(left, right, values, name):
        points = []
        def stamp(us):
            return datetime.fromtimestamp(us / 1000000, UTC).isoformat().replace('+00:00', 'Z')
        interval = (right - left) // len(values)
        assert interval * len(values) == right - left
        for i, at in enumerate(range(left, right, interval)):
            points.append(dict(interval=dict(startTime=stamp(at), endTime=stamp(at + interval)),
                               value=dict(doubleValue=values[i % len(values)])))
        ref = files.obj('trial/' + name, dict(timeSeries=[dict(metric=dict(type='cpu', labels={}),
            resource=resource, metricKind='GAUGE', valueType='DOUBLE', points=points)]))
        return dict(pages=[dict(page_token='', original=ref)], metric='cpu', metric_labels={},
            resource=resource, start_ns=left * 1000, end_ns=right * 1000, interval_ns=interval * 1000)
    baseline = series(start - (end-start), start, (.2, .21), 'baseline')
    trial = series(start, end, (.22, .25), 'cpu')
    allowances = {name: dict(numerator=99, denominator=1) for name in ALLOWANCES} | dict.fromkeys(TOTALS, 10000000)
    component = dict.fromkeys(COMPONENTS, 1)
    pre_allow = files.obj('trial/pre-allowances', dict(**allowances, component_us=component))
    post_allow = files.obj('trial/post-allowances', dict(**allowances, component_us=component))
    components = files.obj('trial/components', {k: [v] for k, v in component.items()})
    measurement, original = shutdown_data()
    original_ref = files.obj('trial/shutdown-span', original)
    measurement['source'] = original_ref
    measured_ref = files.obj('trial/shutdown-measurement', measurement)
    values = peaks()
    values.update(configured_routers=5, configured_publishers=2,
        reads_per_second=dict(numerator=5, denominator=4),
        pending_rows_per_second=dict(numerator=1001, denominator=4),
        configured_reads_per_second=dict(numerator=25, denominator=4),
        configured_pending_rows_per_second=dict(numerator=5005, denominator=4),
        cpu_percent=dict(numerator=26, denominator=1))
    peak_sources = {name: files.obj('trial/peaks/' + name, dict(value=value)) for name, value in values.items()}
    derivations = {name: dict(kind='measured', original=ref, field=['value']) for name, ref in peak_sources.items()}
    limits = files.obj('trial/limits', dict(regions=[dict(region='us-central1', approved=values)], fleet=values))
    limits_review = review(files, limits, 'trial/limits-review')
    serving = [dict(instance=boot, revision=REV, role=role, region='us-central1',
        intervals=[dict(day=DAY, started_at_us=start, flushed_at_us=end)],
        pins=dict(TR_ASYNC_SETTLE_ENABLED='false', TR_ASYNC_SETTLE_PROTECTION='false',
            TR_ASYNC_SETTLE_SHADOW_WORKSPACES='', TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED='false') if role == 'router'
        else dict(TR_ASYNC_SETTLE_NEGOTIATE='off', TR_ASYNC_SETTLE_SHADOW='off'))
        for boot, role in ((BOOT, 'router'), ('enclave', 'enclave'))]
    deployment = files.obj('trial/deploy', dict(serving=serving, maximum_router_instances_by_region={'us-central1': 4}))
    monitor = files.obj('trial/retained-cpu', dict(started_at_us=start, flushed_at_us=end,
        approved_fleet_reads_per_second=8, regions={'us-central1': dict(cpu_peak_percent=21,
            approved_incremental_cpu_percent=5, approved_reads_per_second=8, read_headroom_per_second=10,
            approved_pending_rows_per_second=1300, pending_row_headroom_per_second=1500)}))
    health = files.obj('trial/read-health', dict(started_at_us=start, flushed_at_us=end, regions={'us-central1': [.01]}))
    pre_payload = dict(serving=serving, serving_instance_ids=[BOOT, 'enclave'], workspace_count=1,
        started_at_us=start, flushed_at_us=end, deployment=deployment, monitoring=monitor, read_health=health)
    pre_ref = files.obj('trial/pre-payload', pre_payload)
    pre_result = json.loads(run_retained('pilot_enablement.fleet_budgets_pre', pre_payload, files.originals()).output)
    pre_enable = files.obj('trial/pre-enable', dict(pre_result, completed_at_us=start - 1))
    from trusted_router.async_settle_shadow_v2_a1_deltas import counter_projection as a1_counter
    from trusted_router.async_settle_shadow_v2_projection import counter_projection
    post_payload = dict(pre_enable=pre_enable, workspace_count=1, rows=[dict(
        kind='async_settle_shadow_counter', id=DAY + '/' + BOOT,
        body=counter_projection(a1_counter(counter)))], serving=serving,
        router_instance_ids=[BOOT], maximum_router_instances=4,
        maximum_router_instances_by_region={'us-central1': 4},
        regional_budgets=pre_result['regional_budgets'], approved_fleet_reads_per_second=8,
        freshness_maxima_seconds={k: 0.000001 for k in (*COMPONENTS, 'skew')}, headroom=monitor)
    post_ref = files.obj('trial/post-payload', post_payload)
    source = files.put('trial/retained-source', Path('scripts/async_settle/pilot_enablement.py').read_bytes())
    def spec(ref):
        return dict(payload=ref, source=source, dependencies=[deployment, monitor, health, pre_enable],
            limits=limits, limits_review=limits_review, review=review(files, ref, ref['path'] + '-review'),
            interval=dict(start_us=start, end_us=end), operations=[], load_segments=[dict(
                fields=dict(day=DAY, boot=BOOT, region='us-central1', start_us=start, end_us=end,
                    role='router', contender=True, workspace_count=1), derivations=derivations)])
    plan = dict(configuration=config, baseline=baseline, trial=trial,
        approved_increment=dict(numerator=5, denominator=1), pre_allowances=pre_allow, post_allowances=post_allow,
        post_scope=files.obj('trial/scope', dict(scope='isolated_trial', closed=True, resource=resource, region='us-central1',
            start_us=start, end_us=end, boots=[BOOT])),
        writer_operations=[dict(counter_ref=counter_ref, trace_refs=trace_refs, operation_refs=[native],
            contender=True, start_us=start, end_us=end)], shutdown=[dict(measurement=measured_ref, original=original_ref)],
        components=components, measured_sources=[native, counter_ref, *trace_refs], pre=spec(pre_ref), post=spec(post_ref))
    computed = dict(values)
    computed.update(simultaneous_routers=1, simultaneous_publishers=1,
        startup_operations_1s=1, close_operations_1s=1, export_operations_1s=1,
        cpu_percent=dict(numerator=25, denominator=1))
    def peak(field, name):
        return max((sum(o[field] for o in operations if o['operation'] == name
            and at <= o['at_us'] < at + 1000000) for at in {o['at_us'] for o in operations}), default=0)
    for name, operation in [('trust_rows_per_second', 'workspace_reads'), ('health_rows_per_second', 'health_reads')]:
        computed[name] = dict(numerator=peak('rows', operation), denominator=1)
    for name in ('claim', 'scan', 'publication', 'export'):
        computed[name + '_operations_per_second'] = dict(numerator=peak('logical_count',
            name + 's' if name != 'export' else name), denominator=1)
    computed['export_bytes_per_second'] = dict(numerator=peak('bytes', 'export'), denominator=1)
    for name, value in computed.items():
        derivations[name] = dict(kind='measured', original=files.obj('trial/computed/' + name, dict(value=value)), field=['value'])
    record = dict(scope='isolated_trial', resource=resource, region='us-central1', start_us=start, end_us=end,
        boots=[BOOT], population=[dict(day=DAY, boot=BOOT, start_us=start, end_us=end, region='us-central1', contender=True)], configuration=config, shutdown=plan['shutdown'],
        baseline={k: baseline[k] for k in ('resource', 'start_ns', 'end_ns', 'interval_ns')},
        trial={k: trial[k] for k in ('resource', 'start_ns', 'end_ns', 'interval_ns')})
    plan['component_spans'] = {name: [files.obj('trial/spans/' + name, dict(component=name,
        start_us=start, end_us=start + 1, configuration=config, resource=resource, region='us-central1'))]
        for name in COMPONENTS}
    record['platform'] = files.obj('trial/platform', dict(complete=True, resource=resource,
        boots=[{k: b[k] for k in ('day', 'boot', 'start_us', 'end_us', 'region')} for b in record['population']]))
    record['roles'] = files.obj('trial/roles', dict(complete=True, configuration=config,
        intervals=[{k: b[k] for k in ('day', 'boot', 'start_us', 'end_us', 'contender')} for b in record['population']]))
    record['operation_population'] = files.obj('trial/operation-population', dict(configuration=config,
        complete=True, operations=[dict(day=DAY, boot=BOOT, id=o['id'], operation=o['operation'], at_us=o['at_us'])
            for o in operations if o['operation'] in ('startup', 'close', 'export')]))
    plan['trial_record'] = files.obj('trial/approved-record', record)
    plan['trial_review'] = review(files, plan['trial_record'], 'trial/approved-record-review')
    target = files.put('trial/export-output', b'x')
    receipt = files.obj('trial/export-receipt', dict(object=target, at_us=end - 1, operation='put',
        native_rpcs=1, bytes=1, elapsed_us=1, **{'pass': 1}))
    plan['output_receipts'] = [receipt]
    plan['output_plan'] = files.obj('trial/output-plan', dict(passes=[dict(day=DAY, start_us=start, end_us=end,
        outputs=[dict(object=target, operation='put')], **{'pass': 1})], later_passes=[dict(depends_on=1,
        groups=['coverage', 'roots', 'review', 'report'], native_rpcs=64, export_operations=1000,
        export_bytes=1000000, export_write_bytes=1000000, **{'pass': 2})]))
    plan['output_plan_review'] = review(files, plan['output_plan'], 'trial/output-plan-review')
    return plan



@pytest.fixture(scope='module')
def captured(tmp_path_factory):
    return writer_capture(tmp_path_factory.mktemp('budget-writer'))


def test_budget_complete_originals_to_envelopes(tmp_path, captured):
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    result = CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')
    comparison = json.loads((tmp_path / 'budget/source-comparison.json').read_bytes())
    config = json.loads(files.read(plan['configuration']))
    k = Fraction(config['publishers'])
    assert {n: fraction(v) for n, v in comparison['configured'].items()} == dict(
        selects=k/2 + Fraction(3, 2), unresolved_rows=Fraction(10001, 2), control_reads=k/2+1,
        mutations=1, transactions=k/2+Fraction(1, 2), metadata_bytes=16*5+4*2,
        observer_reads=Fraction(25, 4), pending_rows=Fraction(5005, 4))
    assert {n: fraction(v) for n, v in comparison['cpu'].items()} == dict(
        baseline_peak=21, trial_peak=25, measured_increase=4, approved_increment=5)
    assert comparison['writers'][0]['nominal'] == dict(reads_per_second=dict(numerator=5, denominator=4),
        pending_rows_per_second=dict(numerator=1001, denominator=4))
    operations = json.loads(files.read(plan['writer_operations'][0]['operation_refs'][0]))['operations']
    assert comparison['writers'][0]['totals']['native_rpcs'] == sum(o['native_rpcs'] for o in operations)
    assert comparison['writers'][0]['totals']['native_bytes'] == sum(o['bytes'] for o in operations)
    for stage in ('pre', 'post'):
        emitted = a2.BUDGET_ENVELOPE.parse((tmp_path / ('proof.' + stage + '_budget_sha256')).read_bytes())
        assert emitted == getattr(result, stage)
        graph = PayloadGraph(files.originals())
        dependencies = graph.detail(emitted['retained']['dependencies'], 'files')
        linked = {ref['path']: ref for ref in dependencies}
        from trusted_router.async_settle_shadow_v2_pack_stage import references
        assert all(linked[ref['path']] == ref for ref in references(plan))
        assert 'budget/source-links.json' in linked
        segments = graph.detail(emitted['v2_load']['segments'], 'segments')
        assert len(segments) > 1
        for field in ('startup_operations_1s', 'close_operations_1s', 'export_operations_1s'):
            assert max(s['peaks'][field] for s in segments) == 1
        assert all(s['start_us'] < s['end_us'] for s in segments)
        assert segments[0]['start_us'] == plan[stage]['interval']['start_us']
        assert segments[-1]['end_us'] == plan[stage]['interval']['end_us']
        assert fraction(segments[0]['peaks']['cpu_percent']) == 22
        assert max(fraction(s['peaks']['cpu_percent']) for s in segments) == 25
        assert json.loads(files.read(emitted['v2_load']['limits'])) == json.loads(files.read(plan[stage]['limits']))
    assert result.post['pre_envelope_sha256'] == sha256(result.pre)
    assert json.loads((tmp_path / 'budget/source-links.json').read_bytes()) == plan
    for name, value in json.loads(files.read(plan['pre_allowances'])).items():
        assert result.measurements[name] == value

    changed = copy.deepcopy(plan)
    name = next(iter(changed['component_spans']))
    changed['component_spans'][name] = []
    with pytest.raises(ValueError, match='ComponentSpan'):
        CapturedBudget.build(files, changed, checkpoint='fleet-budgets-pre')

    for damage in ('scope', 'shutdown'):
        files = PackFiles(tmp_path / ('negative-' + damage))
        bad = budget_capture(files, captured)
        if damage == 'scope':
            value = json.loads(files.read(bad['post_scope']))
            value['closed'] = False
            bad['post_scope'] = files.obj('bad/scope', value)
        else:
            value = json.loads(files.read(bad['shutdown'][0]['measurement']))
            value['result_us'] = 0
            bad['shutdown'][0]['measurement'] = files.obj('bad/shutdown', value)
            record = json.loads(files.read(bad['trial_record']))
            record['shutdown'] = bad['shutdown']
            bad['trial_record'] = files.obj('bad/record', record)
            bad['trial_review'] = review(files, bad['trial_record'], 'bad/review')
        with pytest.raises(ValueError):
            CapturedBudget.build(files, bad, checkpoint='fleet-budgets-pre')


@pytest.mark.parametrize('name', a2.PEAKS.fields)
def test_budget_each_peak_source_missing_blocks(tmp_path, captured, name):
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    ref = plan['pre']['load_segments'][0]['derivations'][name]['original']
    (tmp_path / ref['path']).unlink()
    with pytest.raises((ValueError, OSError)):
        CapturedBudget.build(files, plan, checkpoint='fleet-budgets-pre')


def test_budget_measured_max_uses_original_counts(tmp_path, captured):
    from trusted_router.async_settle_shadow_v2_pack_measurements import operation_join
    files = PackFiles(tmp_path)
    plan = budget_capture(files, captured)
    item = copy.deepcopy(plan['writer_operations'][0])
    counter = json.loads(files.read(item['counter_ref']))
    counter['admission_observer'].update(workspace_reads=8, health_reads=16)
    item['counter_ref'] = files.obj('trial/changed-counter', counter)
    original = json.loads(files.read(item['operation_refs'][0]))
    for name, count in (('workspace_reads', 8), ('health_reads', 16)):
        original['operations'].append(dict(id=name, operation=name, logical_count=count, at_us=item['start_us'],
            native_rpcs=count, rows=count, bytes=count*16, elapsed_us=1, slot=None))
    item['operation_refs'] = [files.obj('trial/changed-operations', original)]
    joined = operation_join(files, **item)
    duration = Fraction(item['end_us'] - item['start_us'], 1000000)
    assert fraction(joined['nominal']['reads_per_second']) == max(Fraction(1, 4), 8/duration) + max(1, 16/duration)
    assert fraction(joined['nominal']['pending_rows_per_second']) == max(Fraction(1, 4), 8/duration)*1001
