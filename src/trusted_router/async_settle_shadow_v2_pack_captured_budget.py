"""Complete captured budget records, with retained allowances kept separate."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_budget import (
    budget_measurements,
    envelope,
    fraction,
    rational,
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
        destination = files
        prefix = plan.get('output_prefix', '' if checkpoint in ('fleet-budgets-pre', 'transport') else 'production/')
        if prefix:
            from trusted_router.async_settle_shadow_v2_pack_originals import ScopedOutputs
            files = ScopedOutputs(files.root, refs=files.refs, prefix=prefix)
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
        expected_scope = 'isolated_trial' if checkpoint in ('fleet-budgets-pre', 'transport') else 'production_warmup'
        require(scope['scope'] == expected_scope and scope['closed'] is True and bool(scope['resource']), 'artifact_gap')
        require(0 < scope['end_us'] - scope['start_us'] <= 3600000000 or expected_scope == 'production_warmup',
                'artifact_gap')
        from trusted_router.async_settle_shadow_v2_pack_trial import (
            changed_intervals,
            component_spans,
            native_peaks,
            trial_record,
        )
        population = trial_record(files, plan, scope)
        component_spans(files, plan)
        joins = [operation_join(files, **item, role_intervals=[s for s in population['segments']
            if s['boot'] == retained_json(files.read(item['counter_ref']))['boot']])
            for item in plan['writer_operations']]
        require(bool(joins), 'artifact_gap')
        for item in plan['shutdown']:
            shutdown(retained_json(files.read(item['measurement'])), retained_json(files.read(item['original'])))
        require(bool(plan['shutdown']), 'artifact_gap')
        kwargs = {}
        original_pre = None
        if expected_scope == 'production_warmup':
            from trusted_router import async_settle_shadow_v2_a2_evidence as a2
            from trusted_router.async_settle_shadow_v2_collect_ports import approval
            from trusted_router.async_settle_shadow_v2_gate_payloads import PayloadGraph
            from trusted_router.async_settle_shadow_v2_pack_plan import check_plan
            require('approved_pre' in plan, 'MissingApprovedPre')
            prior = plan['approved_pre']
            require(retained_json(files.read(prior['inputs_ref'])) == prior['inputs'], 'ApprovedPreGraph')
            approval(files, prior['inputs_review'], prior['inputs_ref'])
            originals = check_plan(files, prior['inputs'])
            require(prior['envelope'] in originals, 'ApprovedPrePopulation')
            approval(files, prior['review'], prior['envelope'])
            raw = files.read(prior['envelope'])
            original_pre = a2.BUDGET_ENVELOPE.parse(raw)
            require(original_pre['stage'] == 'pre', 'ApprovedPreStage')
            graph = PayloadGraph({ref['path']: files.read(ref) for ref in originals})
            graph.materialize({'proof.pre_budget_sha256': original_pre})
            require(not graph.issues, 'ApprovedPreGraph')
            destination.put('proof.pre_budget_sha256', raw)
            require(files.read(prior['allowances']) == files.read(plan['pre_allowances']), 'ApprovedPreAllowances')
            require(files.read(prior['configuration']) == files.read(plan['configuration']), 'ApprovedPreConfiguration')
        peak_cache: dict[tuple[Any, ...], dict[str, Any]] = {}
        for stage in (('pre', 'post') if original_pre is None else ('post',)):
            spec = dict(plan[stage])
            spec['operations'] = [retained_json(files.read(ref)) for ref in plan.get('output_receipts', [])]
            spec['load_segments'] = [{**segment['fields'], 'peaks': derived_peaks(files, segment['derivations'])}
                                      for segment in spec['load_segments']]
            require(bool(spec['load_segments']), 'artifact_gap')
            for segment in spec['load_segments']:
                peaks = segment['peaks']
                key = tuple(segment[k] for k in ('day', 'boot', 'region', 'role', 'contender', 'workspace_count', 'start_us', 'end_us'))
                if key not in peak_cache:
                    peak_cache[key] = native_peaks(files, plan, segment, config, projection, cpu)
                require(peaks == peak_cache[key], 'MeasuredPeaks')
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
            pieces = []
            for segment in spec['load_segments']:
                for piece in changed_intervals(files, plan, segment):
                    # Retain a conservative parent window bound for operations.
                    # CPU follows its original sample boundaries independently.
                    peaks = {**segment['peaks'], 'cpu_percent': rational(max(
                        fraction(sample['percent']) for sample in trial['samples']
                        if sample['start_ns'] < piece['end_us'] * 1000 and sample['end_ns'] > piece['start_us'] * 1000))}
                    pieces.append({**piece, 'peaks': peaks})
            spec['load_segments'] = pieces
            kwargs[stage] = spec
        allowances = retained_json(files.read(plan['pre_allowances']))
        ceilings = files.obj('budget/reviewed-ceilings.json',
            {k: v for k, v in allowances.items() if k != 'component_us'})
        measured = budget_measurements(files, reviewed=ceilings,
            component_source=plan['components'], measured_sources=plan['measured_sources'])
        files.obj('budget/source-comparison.json', dict(configured=projection, cpu=cpu, writers=joins, post_scope=scope, segments=population['segments']))
        files.obj('budget/source-links.json', plan)
        linked = {ref['path']: ref for ref in references(plan)}
        for path in ('budget/source-comparison.json', 'budget/source-links.json',
                     'loading/budget-measurements.json'):
            linked[prefix + path] = files.refs[prefix + path]
        for spec in kwargs.values():
            spec['dependencies'] = list({ref['path']: ref for ref in
                [*spec['dependencies'], *linked.values()]}.values())
        pre = original_pre if original_pre is not None else envelope(files, stage='pre', **kwargs['pre'])
        post = envelope(files, stage='post', pre=pre, **kwargs['post'])
        if prefix:
            from trusted_router.detached_jws import canonical
            if original_pre is None:
                destination.put('proof.pre_budget_sha256', canonical(pre))
            destination.put('proof.post_budget_sha256', canonical(post))
        return cls(pre, post, measured, tuple(files.refs.values()))
