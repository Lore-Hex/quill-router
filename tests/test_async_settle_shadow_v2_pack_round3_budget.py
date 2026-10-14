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
    START,
    peaks,
    review,
    shutdown_data,
)
from tests.test_async_settle_shadow_v2_pack_round2 import monitoring
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
    native = files.obj('trial/native-operations', dict(day=DAY, boot=BOOT, start_us=start,
        end_us=end, complete=True, operations=operations))
    config = files.obj('trial/configuration', dict(workspace_count=1, fleet_cap=4,
        regional_caps={'us-central1': 4}, surge=1, publishers=2))
    baselines = monitoring(files, 'trial/baseline', (.2, .21))
    trials = monitoring(files, 'trial/cpu', (.22, .25), minute=1)
    def series(pages, minute):
        return dict(pages=pages, metric='cpu', metric_labels={},
            resource=dict(type='spanner_instance', labels=dict(node='one')),
            start_ns=(START + minute * 60000000) * 1000 + 1,
            end_ns=(START + minute * 60000000 + 2000000) * 1000 + 1, interval_ns=1000000000)
    allowances = {name: dict(numerator=99, denominator=1) for name in ALLOWANCES} | dict.fromkeys(TOTALS, 99)
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
    return dict(configuration=config, baseline=series(baselines, 0), trial=series(trials, 1),
        approved_increment=dict(numerator=5, denominator=1), pre_allowances=pre_allow, post_allowances=post_allow,
        post_scope=files.obj('trial/scope', dict(scope='isolated_trial', closed=True, resource='fake-native-database',
            start_us=start, end_us=end, boots=[BOOT])),
        writer_operations=[dict(counter_ref=counter_ref, trace_refs=trace_refs, operation_refs=[native],
            contender=True, start_us=start, end_us=end)], shutdown=[dict(measurement=measured_ref, original=original_ref)],
        components=components, measured_sources=[native, counter_ref, *trace_refs], pre=spec(pre_ref), post=spec(post_ref))


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
        for name, derivation in plan[stage]['load_segments'][0]['derivations'].items():
            assert segments[0]['peaks'][name] == json.loads(files.read(derivation['original']))['value']
        assert fraction(segments[0]['peaks']['cpu_percent']) == 21 + 5
        assert json.loads(files.read(emitted['v2_load']['limits'])) == json.loads(files.read(plan[stage]['limits']))
    assert result.post['pre_envelope_sha256'] == sha256(result.pre)
    assert json.loads((tmp_path / 'budget/source-links.json').read_bytes()) == plan
    for name, value in json.loads(files.read(plan['pre_allowances'])).items():
        assert result.measurements[name] == value


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
