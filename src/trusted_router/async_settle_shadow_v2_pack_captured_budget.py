"""Complete captured budget records, with retained allowances kept separate."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_budget import (
    budget_measurements,
    envelope,
    fraction,
    shutdown,
)
from trusted_router.async_settle_shadow_v2_pack_measurements import operation_join
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_pack_sources import (
    compare_cpu,
    derived_peaks,
    equal_allowances,
    monitoring_series,
    publisher_projection,
)
from trusted_router.async_settle_shadow_v2_pack_stage import references
from trusted_router.async_settle_shadow_v2_schema import require


@dataclass(frozen=True)
class CapturedBudget:
    pre: dict[str, Any]
    post: dict[str, Any]
    measurements: dict[str, Any]
    sources: tuple[dict[str, Any], ...]

    @classmethod
    def build(cls, files: PackFiles, plan: dict[str, Any], *, checkpoint: str) -> CapturedBudget:
        config = retained_json(files.read(plan['configuration']))
        require(type(config['workspace_count']) is int and config['workspace_count'] == 1
                and bool(config['regional_caps']) and all(type(v) is int and v > 0 for v in config['regional_caps'].values())
                and config['fleet_cap'] == sum(config['regional_caps'].values()),
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
            for segment in spec['load_segments']:
                peaks = segment['peaks']
                require(peaks['configured_routers'] == config['fleet_cap'] + config['surge']
                    and peaks['configured_publishers'] == config['publishers'], 'artifact_gap')
                require(peaks['configured_reads_per_second'] == projection['observer_reads'] and
                    peaks['configured_pending_rows_per_second'] == projection['pending_rows'], 'artifact_gap')
                matched = [join for item, join in zip(plan['writer_operations'], joins, strict=True)
                    if retained_json(files.read(item['counter_ref']))['boot'] == segment['boot']]
                require(bool(matched), 'artifact_gap')
                for join in matched:
                    for field in ('reads_per_second', 'pending_rows_per_second'):
                        require(fraction(peaks[field]) >= fraction(join['nominal'][field]), 'artifact_gap')
                require(fraction(cpu['trial_peak']) <= fraction(peaks['cpu_percent']) <=
                    fraction(cpu['baseline_peak']) + fraction(cpu['approved_increment']), 'fleet_headroom_gap')
            if stage == 'post':
                require(spec['interval'] == dict(start_us=scope['start_us'], end_us=scope['end_us']), 'artifact_gap')
                require({s['boot'] for s in spec['load_segments']} == set(scope['boots']), 'artifact_gap')
            kwargs[stage] = spec
        allowances = retained_json(files.read(plan['pre_allowances']))
        ceilings = files.obj('budget/reviewed-ceilings.json',
            {k: v for k, v in allowances.items() if k != 'component_us'})
        measured = budget_measurements(files, reviewed=ceilings,
            component_source=plan['components'], measured_sources=plan['measured_sources'])
        files.obj('budget/source-comparison.json', dict(configured=projection, cpu=cpu, writers=joins, post_scope=scope))
        files.obj('budget/source-links.json', plan)
        linked = {ref['path']: ref for ref in references(plan)}
        for path in ('budget/source-comparison.json', 'budget/source-links.json',
                     'loading/budget-measurements.json'):
            linked[path] = files.refs[path]
        for spec in kwargs.values():
            spec['dependencies'] = list({ref['path']: ref for ref in
                [*spec['dependencies'], *linked.values()]}.values())
        pre = envelope(files, stage='pre', **kwargs['pre'])
        post = envelope(files, stage='post', pre=pre, **kwargs['post'])
        return cls(pre, post, measured, tuple(files.refs.values()))
