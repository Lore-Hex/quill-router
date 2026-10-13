"""Measured component bounds and separately reviewed budget ceilings."""
from __future__ import annotations

from fractions import Fraction
from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_collect_ports import TAGS, approval
from trusted_router.async_settle_shadow_v2_collect_shutdown import durations
from trusted_router.async_settle_shadow_v2_gate_inputs import (
    ALLOWANCES,
    BUDGET_MEASUREMENTS,
    COMPONENTS,
    TOTALS,
)
from trusted_router.async_settle_shadow_v2_gate_load import COMPONENT_BOUNDS
from trusted_router.async_settle_shadow_v2_gate_loading import LIMITS, retained_json
from trusted_router.async_settle_shadow_v2_pack_history import retained_call
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import day_at, require, sha256

DAY_US = 86400000000


def fraction(value: dict[str, int]) -> Fraction:
    a2.RATIONAL(value)
    return Fraction(value['numerator'], value['denominator'])


def rational(value: Fraction) -> dict[str, int]:
    return dict(numerator=value.numerator, denominator=value.denominator)


def nominal(workspaces: int, workspace_reads: int, health_reads: int, elapsed_us: int) -> dict[str, Any]:
    require(type(workspaces) is int and 1 <= workspaces <= 32 and elapsed_us > 0, 'artifact_gap')
    require(type(workspace_reads) is int and workspace_reads >= 0 and type(health_reads) is int and health_reads >= 0)
    duration = Fraction(elapsed_us, 1000000)
    workspace = max(Fraction(workspaces, 4), workspace_reads / duration)
    health = max(Fraction(1), health_reads / duration)
    return dict(reads_per_second=rational(workspace + health), pending_rows_per_second=rational(workspace * 1001))


def measured_components(samples: dict[str, list[int]]) -> dict[str, int]:
    require(set(samples) == set(COMPONENTS), 'artifact_gap')
    result = {}
    for name, values in samples.items():
        require(bool(values) and all(type(v) is int and v >= 0 for v in values), 'artifact_gap')
        bound = max(values)
        require(bound <= Fraction(str(COMPONENT_BOUNDS[name])) * 1000000, 'freshness_budget_gap')
        result[name] = bound
    require(sum(result[k] for k in ('publisher_period', 'publisher_jitter', 'publication',
                'poll_period', 'poll_jitter', 'install')) <= 4250000, 'freshness_budget_gap')
    require(sum(result[k] for k in ('workspace_period', 'workspace_jitter', 'workspace_install'))
            <= 4750000, 'freshness_budget_gap')
    return result


def cpu_peak(series: dict[str, Any], *, node: str, start_us: int, end_us: int,
             sample_interval_us: int, approved_increment: dict[str, int]) -> Fraction:
    """Captured Monitoring intervals must cover the actual node without gaps."""
    require(series['source'] == 'monitoring.timeSeries.list' and series['node'] == node, 'artifact_gap')
    require(series['sample_interval_us'] == sample_interval_us and sample_interval_us > 0, 'artifact_gap')
    require(start_us < end_us and series['complete'] is True, 'artifact_gap')
    cursor = start_us
    peak = Fraction(0)
    require(bool(series['samples']), 'artifact_gap')
    for sample in series['samples']:
        require(sample['start_us'] == cursor and sample['end_us'] - cursor == sample_interval_us
                and sample['end_us'] <= end_us, 'artifact_gap')
        value = fraction(sample['percent'])
        require(value <= 100, 'artifact_gap')
        peak = max(peak, value)
        cursor = sample['end_us']
    require(cursor == end_us, 'artifact_gap')
    require(fraction(approved_increment) > 0, 'fleet_headroom_gap')
    require(peak + fraction(approved_increment) < 45, 'fleet_headroom_gap')
    return peak


def shutdown(measurement: dict[str, Any], original: dict[str, Any]) -> int:
    a2.SHUTDOWN_MEASUREMENT(measurement)
    measured = durations(original)
    require(all(measurement[key] == value for key, value in measured.items()), 'artifact_gap')
    require(measurement['day'] == original['day'] and measurement['boot'] == original['boot'], 'artifact_gap')
    require(original['complete'] is True and original['outcome'] == measurement['outcome'] == 'graceful', 'artifact_gap')
    require(measurement['close_us'] <= 2000000 and measurement['result_us'] <= 2000000
            and measurement['close_us'] + measurement['result_us'] <= measurement['final_pass_us'] <= 4000000,
            'artifact_gap')
    total = measurement['preparation_us'] + measurement['export_wait_us'] + measurement['final_pass_us']
    require(total <= min(measurement['grace_us'], 9000000), 'artifact_gap')
    return total


def segments(intervals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Input intervals are measured rate/role intervals, split again at UTC days."""
    result = []
    for interval in intervals:
        a2.LOAD_SEGMENT(interval)
        require(all(value is not None for value in interval['peaks'].values()), 'artifact_gap')
        start, end = interval['start_us'], interval['end_us']
        require(start < end, 'artifact_gap')
        while start < end:
            right = min(end, (start // DAY_US + 1) * DAY_US)
            result.append({**interval, 'day': day_at(start), 'start_us': start, 'end_us': right})
            start = right
    from trusted_router.async_settle_shadow_v2_a2_roots import entry_root
    entry_root(result, stream='segments')
    return sorted(result, key=lambda value: (value['day'], value['boot'], value['start_us']))


def budget_measurements(files: PackFiles, *, reviewed: dict[str, Any],
                        component_source: dict[str, Any], measured_sources: list[dict[str, Any]]) -> dict[str, Any]:
    ceilings = retained_json(files.read(reviewed))
    components = measured_components(retained_json(files.read(component_source)))
    require(bool(measured_sources), 'artifact_gap')
    for ref in measured_sources:
        files.read(ref)
    require(set(ceilings) == set(ALLOWANCES) | set(TOTALS), 'artifact_gap')
    value = {**ceilings, 'component_us': components}
    files.obj('loading/budget-measurements.json', value, BUDGET_MEASUREMENTS)
    files.detail('budget/measured-sources', [reviewed, component_source, *measured_sources], 'files')
    return value


def envelope(files: PackFiles, *, stage: str, payload: dict[str, Any], source: dict[str, Any],
             dependencies: list[dict[str, Any]], limits: dict[str, Any], review: dict[str, Any],
             limits_review: dict[str, Any], interval: dict[str, int],
             load_segments: list[dict[str, Any]], operations: list[dict[str, Any]],
             pre: dict[str, Any] | None = None) -> dict[str, Any]:
    require(stage in ('pre', 'post') and (pre is None) == (stage == 'pre'), 'artifact_gap')
    approved = approval(files, review, payload)
    approved_limits = approval(files, limits_review, limits)
    limit_values = LIMITS.parse(files.read(limits))
    require(all(v is not None for peak in [limit_values['fleet'],
            *(r['approved'] for r in limit_values['regions'])] for v in peak.values()), 'artifact_gap')
    if pre is not None:
        require(files.read(pre['v2_load']['limits']) == files.read(limits), 'artifact_gap')
        old = retained_json(files.read(pre['output']))
        new = retained_json(files.read(payload))
        for key in ('workspace_count', 'regional_budgets', 'approved_fleet_reads_per_second', 'maximum_router_instances_by_region'):
            require(key in old and key in new and old[key] == new[key], 'artifact_gap')
    for op in operations:
        a2.EXPORT_OPERATION(op)
        files.read(op['object'])
        require(interval['start_us'] <= op['at_us'] < interval['end_us'], 'artifact_gap')
        require(op['bytes'] == (0 if op['operation'] == 'delete' else op['object']['bytes']), 'artifact_gap')
    load = dict(**TAGS, interval=interval, segments=files.detail('budget/' + stage + '/segments',
                segments(load_segments), 'segments'), operations=files.detail('budget/' + stage + '/operations',
                operations, 'operations'), limits=limits, approval=approved_limits)
    retained, output, _ = retained_call(files, entry='pilot_enablement.fleet_budgets' + ('_pre' if stage == 'pre' else ''),
        payload=payload, dependencies=dependencies, source=source, prefix='budget/' + stage)
    value = dict(**TAGS, stage=stage, retained=retained, output=output, pre_envelope_sha256=sha256(pre) if pre else None,
                 approval=approved, v2_load=load)
    files.obj('proof.' + stage + '_budget_sha256', value, a2.BUDGET_ENVELOPE)
    return value


def export_costs(accounting: list[Any], sink_operations: list[dict[str, Any]], *,
                 approved: dict[str, Any]) -> dict[str, int]:
    """Native reads and sink receipts stay separate from reviewed ceilings."""
    require(bool(accounting) and all(result.complete for result in accounting), 'artifact_gap')
    for operation in sink_operations:
        a2.EXPORT_OPERATION(operation)
    totals = dict(native_rpcs=sum(result.native_rpcs for result in accounting)
                  + sum(operation['native_rpcs'] for operation in sink_operations),
        source_body_bytes=sum(result.body_bytes for result in accounting),
        export_passes=len(accounting), export_operations=sum(result.operations for result in accounting)
            + len(sink_operations), export_bytes=sum(result.body_bytes for result in accounting)
            + sum(operation['bytes'] for operation in sink_operations),
        export_write_bytes=sum(operation['bytes'] for operation in sink_operations if operation['operation'] == 'put'))
    for key in ('export_passes', 'export_operations', 'export_bytes', 'export_write_bytes'):
        require(type(approved[key]) is int and totals[key] <= approved[key], 'evidence_export_budget_gap')
    # The reviewed measurement source must retain native RPC capacity separately.
    require(type(approved['native_rpcs']) is int and totals['native_rpcs'] <= approved['native_rpcs'],
            'evidence_export_budget_gap')
    return totals
