"""Source-joined budget assembly over captured trial and writer originals."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_budget import (
    budget_measurements,
    envelope,
    nominal,
    shutdown,
)
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_pack_sources import (
    compare_cpu,
    derived_peaks,
    equal_allowances,
    monitoring_series,
    publisher_projection,
)
from trusted_router.async_settle_shadow_v2_schema import require


def operation_join(files: Files, *, counter_ref: dict[str, Any], trace_refs: list[dict[str, Any]],
                   operation_refs: list[dict[str, Any]], contender: bool,
                   start_us: int, end_us: int) -> dict[str, Any]:
    counter = a1.COUNTER.parse(files.read(counter_ref))
    require((counter['started_at_us'], counter['flushed_at_us']) == (start_us, end_us)
            and counter['closed'], 'artifact_gap')
    traces = []
    previous = None
    for ref in trace_refs:
        page = health.PUBLISHER_TRACE_PAGE.parse(files.read(ref))
        require(page['previous_sha256'] == previous, 'publisher_trace_gap')
        from trusted_router.async_settle_shadow_v2_schema import sha256
        previous = sha256(page)
        traces.extend(page['entries'])
    first: int | None = (start_us + 1999999) // 2000000
    last: int | None = (end_us - 1) // 2000000
    if not contender or first is None or last is None or first > last:
        first = last = None
    health.validate_publisher_traces(counter['publisher'], traces, day=counter['day'], boot=counter['boot'],
                                     first_slot=first, last_slot=last)
    require(bool(operation_refs), 'artifact_gap')
    totals: Counter[str] = Counter()
    slots: Counter[int] = Counter()
    ids = set()
    for ref in operation_refs:
        original = retained_json(files.read(ref))
        require(original['complete'] is True and original['boot'] == counter['boot']
                and original['day'] == counter['day'], 'artifact_gap')
        require(original['start_us'] == start_us and original['end_us'] == end_us, 'artifact_gap')
        for operation in original['operations']:
            require(operation['id'] not in ids and start_us <= operation['at_us'] < end_us, 'artifact_gap')
            ids.add(operation['id'])
            require(operation['operation'] in ('workspace_reads', 'health_reads', 'claims', 'scans',
                'publications', 'begin', 'commit', 'rollback', 'stream', 'metadata', 'mutation', 'cleanup'), 'artifact_gap')
            for field in ('logical_count', 'native_rpcs', 'rows', 'bytes', 'elapsed_us'):
                require(type(operation[field]) is int and operation[field] >= 0, 'artifact_gap')
            totals[operation['operation']] += operation['logical_count']
            totals['native_rpcs'] += operation['native_rpcs']
            totals['native_bytes'] += operation['bytes']
            if operation['slot'] is not None:
                require(any(t['slot'] == operation['slot'] for t in traces), 'publisher_accounting_gap')
                slots[operation['slot']] += operation['native_rpcs']
    require(all(slots[t['slot']] == t['rpc_count'] for t in traces), 'publisher_accounting_gap')
    require(all(totals[k] == counter['publisher'][k] for k in ('claims', 'scans', 'publications')),
            'publisher_accounting_gap')
    obs = counter['admission_observer']
    require(all(totals[k] == obs[k] for k in ('workspace_reads', 'health_reads')), 'observer_budget_gap')
    return dict(totals=dict(totals), nominal=nominal(1, obs['workspace_reads'], obs['health_reads'], end_us - start_us))


@dataclass(frozen=True)
class BudgetSourceAssembly:
    pre: dict[str, Any]
    post: dict[str, Any]
    measurements: dict[str, Any]
    sources: tuple[dict[str, Any], ...]

    @classmethod
    def build(cls, files: PackFiles, plan: dict[str, Any], *, checkpoint: str) -> BudgetSourceAssembly:
        config = retained_json(files.read(plan['configuration']))
        require(config['workspace_count'] == 1 and config['fleet_cap'] == sum(config['regional_caps'].values()),
                'artifact_gap')
        projection = publisher_projection(fleet_cap=config['fleet_cap'], surge=config['surge'],
                                          publishers=config['publishers'])
        baseline = monitoring_series(files, **plan['baseline'])
        trial = monitoring_series(files, **plan['trial'])
        cpu = compare_cpu(baseline, trial, plan['approved_increment'])
        equal_allowances(files, plan['pre_allowances'], plan['post_allowances'])
        scope = retained_json(files.read(plan['post_scope']))
        expected_scope = 'isolated_trial' if checkpoint == 'fleet-budgets-pre' else 'production_warmup'
        require(scope['scope'] == expected_scope and scope['closed'] is True and bool(scope['resource']), 'artifact_gap')
        require(0 < scope['end_us'] - scope['start_us'] <= 3600000000 or expected_scope == 'production_warmup',
                'artifact_gap')
        joins = [operation_join(files, **item) for item in plan['writer_operations']]
        require(bool(joins), 'artifact_gap')
        for item in plan['shutdown']:
            shutdown(retained_json(files.read(item['measurement'])), retained_json(files.read(item['original'])))
        require(bool(plan['shutdown']), 'artifact_gap')
        kwargs = {}
        for stage in ('pre', 'post'):
            spec = dict(plan[stage])
            spec['load_segments'] = [{**segment['fields'], 'peaks': derived_peaks(files, segment['derivations'])}
                                      for segment in spec['load_segments']]
            require(bool(spec['load_segments']), 'artifact_gap')
            if stage == 'post':
                require(spec['interval'] == dict(start_us=scope['start_us'], end_us=scope['end_us']), 'artifact_gap')
                require({s['boot'] for s in spec['load_segments']} == set(scope['boots']), 'artifact_gap')
            kwargs[stage] = spec
        measured = budget_measurements(files, reviewed=plan['pre_allowances'],
            component_source=plan['components'], measured_sources=plan['measured_sources'])
        files.obj('budget/source-comparison.json', dict(configured=projection, cpu=cpu, writers=joins, post_scope=scope))
        pre = envelope(files, stage='pre', **kwargs['pre'])
        post = envelope(files, stage='post', pre=pre, **kwargs['post'])
        return cls(pre, post, measured, tuple(files.refs.values()))
