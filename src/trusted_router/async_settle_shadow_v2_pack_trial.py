"""Join trial records to their configured population and timed measurements."""
from __future__ import annotations

from collections import Counter
from fractions import Fraction
from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import approval
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_budget import fraction, nominal, rational
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import day_at, require


def trial_record(files: PackFiles, plan: dict[str, Any], scope: dict[str, Any]) -> dict[str, Any]:
    record = retained_json(files.read(plan['trial_record']))
    approval(files, plan['trial_review'], plan['trial_record'])
    require(record['configuration'] == plan['configuration'], 'TrialConfiguration')
    require(all(scope[k] == record[k] for k in ('scope', 'resource', 'region', 'start_us', 'end_us', 'boots')),
            'TrialPopulation')
    for name in ('baseline', 'trial'):
        require({k: plan[name][k] for k in ('resource', 'start_ns', 'end_ns', 'interval_ns')} == record[name],
                'TrialInterval')
    require(record['trial']['resource'] == record['resource'] and
            record['trial']['start_ns'] == record['start_us'] * 1000 and
            record['trial']['end_ns'] == record['end_us'] * 1000, 'TrialInterval')
    expected = {(b['day'], b['boot']) for b in record['population']}
    require(len(expected) == len(record['population']) and {b for _, b in expected} == set(record['boots']),
            'TrialPopulation')
    counters = [retained_json(files.read(item['counter_ref'])) for item in plan['writer_operations']]
    require({(c['day'], c['boot']) for c in counters} == expected and len(counters) == len(expected),
            'TrialPopulation')
    for item, counter in zip(plan['writer_operations'], counters, strict=True):
        require(counter['region'] == record['region'] and
            (item['start_us'], item['end_us']) == (record['start_us'], record['end_us']), 'TrialInterval')
        for ref in item['operation_refs']:
            original = retained_json(files.read(ref))
            require(original['configuration'] == record['configuration'] and
                    original['resource'] == record['resource'] and original['region'] == record['region'],
                    'TrialOperation')
    pairs = []
    for item in plan['shutdown']:
        measured = retained_json(files.read(item['measurement']))
        original = retained_json(files.read(item['original']))
        pairs.append((measured['day'], measured['boot']))
        require((original['day'], original['boot']) == pairs[-1], 'ShutdownPopulation')
        if measured['source'] != item['original']:
            source = retained_json(files.read(measured['source']))
            require(source == {k: v for k, v in measured.items() if k != 'source'}, 'ShutdownSource')
    require(set(pairs) == expected and len(pairs) == len(expected), 'ShutdownPopulation')
    require(plan['shutdown'] == record['shutdown'], 'ShutdownOriginals')
    if 'shutdown_inventory' in record:
        from trusted_router import async_settle_shadow_v2_a2_evidence as a2
        from trusted_router.async_settle_shadow_v2_a2_roots import entry_root
        captured = retained_json(files.read(record['shutdown_inventory']))
        envelope = a2.SHUTDOWN_INVENTORY.parse(files.read(captured['envelope']))
        require(envelope['complete'] and captured['complete'] and
            entry_root(captured['entries'], stream='shutdown_writers') == envelope['writers']['root'],
            'ShutdownInventory')
        require({(e['day'], e['boot']) for e in captured['entries']} == expected and
            len(captured['entries']) == len(expected), 'ShutdownPopulation')
        require([e['measurement'] for e in captured['entries']] == [p['measurement'] for p in plan['shutdown']],
            'ShutdownOriginals')
        for entry in captured['entries']:
            files.read(entry['original_close'])
            files.read(entry['close_result'])
    for stage in ('pre', 'post'):
        require(plan[stage]['interval'] == dict(start_us=record['start_us'], end_us=record['end_us']),
                'TrialInterval')
        segments = [s['fields'] for s in plan[stage]['load_segments']]
        require({(s['day'], s['boot']) for s in segments} == expected, 'TrialPopulation')
        for boot in record['boots']:
            cursor = record['start_us']
            for segment in sorted((s for s in segments if s['boot'] == boot), key=lambda s: s['start_us']):
                require(segment['start_us'] == cursor, 'TrialSegmentCoverage')
                cursor = segment['end_us']
            require(cursor == record['end_us'], 'TrialSegmentCoverage')
        for segment in segments:
            require(segment['role'] == 'router' and segment['workspace_count'] == 1, 'TrialPopulation')
            require(segment['region'] == record['region'] and segment['day'] == day_at(segment['start_us'])
                    and record['start_us'] <= segment['start_us'] < segment['end_us'] <= record['end_us'],
                    'TrialInterval')
    return record


def native_peaks(files: PackFiles, plan: dict[str, Any], fields: dict[str, Any],
                 config: dict[str, Any], projection: dict[str, Any], cpu: dict[str, Any]) -> dict[str, Any]:
    """Compute peaks from timed original operations, never from peak projections."""
    start, end = fields['start_us'], fields['end_us']
    operations: list[dict[str, Any]] = []
    for item in plan['writer_operations']:
        counter = retained_json(files.read(item['counter_ref']))
        if counter['boot'] == fields['boot']:
            for ref in item['operation_refs']:
                original = retained_json(files.read(ref))
                operations.extend(o for o in original['operations'] if start <= o['at_us'] < end)
    # Sliding one-second buckets include bursts across an integral-second boundary.
    buckets: list[Counter[str]] = [Counter()]
    for left in sorted({o['at_us'] for o in operations}):
        bucket: Counter[str] = Counter()
        for op in operations:
            if left <= op['at_us'] < left + 1000000:
                bucket[op['operation']] += op['logical_count']
                bucket['bytes:' + op['operation']] += op['bytes']
                bucket['rows:' + op['operation']] += op['rows']
        buckets.append(bucket)
    def peak(name: str) -> int:
        return max(b[name] for b in buckets)
    observed = nominal(fields['workspace_count'], peak('workspace_reads'), peak('health_reads'), 1000000)
    from trusted_router.async_settle_shadow_v2_pack_sources import monitoring_series
    series = monitoring_series(files, **plan['trial'])
    cpu_peak = max(fraction(sample['percent']) for sample in series['samples']
        if sample['start_ns'] < end * 1000 and sample['end_ns'] > start * 1000)
    require(cpu_peak <= fraction(cpu['trial_peak']), 'MeasuredCpuPeak')
    values = dict(simultaneous_routers=int(fields['role'] == 'router'),
        simultaneous_publishers=int(fields['contender']), configured_routers=config['fleet_cap'] + config['surge'],
        configured_publishers=config['publishers'], startup_operations_1s=peak('startup'),
        close_operations_1s=peak('close'), export_operations_1s=peak('export'),
        reads_per_second=observed['reads_per_second'], pending_rows_per_second=observed['pending_rows_per_second'],
        trust_rows_per_second=rational(Fraction(peak('rows:workspace_reads'))),
        health_rows_per_second=rational(Fraction(peak('rows:health_reads'))),
        claim_operations_per_second=rational(Fraction(peak('claims'))),
        scan_operations_per_second=rational(Fraction(peak('scans'))),
        publication_operations_per_second=rational(Fraction(peak('publications'))),
        export_operations_per_second=rational(Fraction(peak('export'))),
        export_bytes_per_second=rational(Fraction(peak('bytes:export'))),
        configured_reads_per_second=projection['observer_reads'],
        configured_pending_rows_per_second=projection['pending_rows'], cpu_percent=rational(cpu_peak))
    require(fraction(values['cpu_percent']) >= 0, 'artifact_gap')
    return values


def component_spans(files: PackFiles, plan: dict[str, Any]) -> None:
    """Each component sample is the duration of an original span in this trial."""
    from trusted_router.async_settle_shadow_v2_gate_inputs import COMPONENTS
    record = retained_json(files.read(plan['trial_record']))
    projected = retained_json(files.read(plan['components']))
    require(set(plan['component_spans']) == set(COMPONENTS), 'ComponentSpans')
    for name in COMPONENTS:
        values = []
        for ref in plan['component_spans'][name]:
            span = retained_json(files.read(ref))
            require(span['component'] == name and span['configuration'] == record['configuration']
                    and span['resource'] == record['resource'] and span['region'] == record['region'],
                    'ComponentSpanScope')
            require(record['start_us'] <= span['start_us'] <= span['end_us'] <= record['end_us'],
                    'ComponentSpanInterval')
            values.append(span['end_us'] - span['start_us'])
        require(bool(values) and projected[name] == values, 'ComponentSpanProjection')


def changed_intervals(files: PackFiles, plan: dict[str, Any], fields: dict[str, Any]) -> list[dict[str, Any]]:
    """Split at native operation buckets, CPU samples, UTC and supplied role edges."""
    left, right = fields['start_us'], fields['end_us']
    boundaries = {left, right}
    for item in plan['writer_operations']:
        for ref in item['operation_refs']:
            for operation in retained_json(files.read(ref))['operations']:
                at = operation['at_us']
                boundaries.update(t for t in (at, at + 1000000) if left < t < right)
    sample = plan['trial']
    for at_ns in range(sample['start_ns'], sample['end_ns'], sample['interval_ns']):
        if left * 1000 < at_ns < right * 1000:
            require(at_ns % 1000 == 0, 'TrialSegmentBoundary')
            boundaries.add(at_ns // 1000)
    for at in range((left // 86400000000 + 1) * 86400000000, right, 86400000000):
        boundaries.add(at)
    ordered = sorted(boundaries)
    return [{**fields, 'start_us': start, 'end_us': end} for start, end in zip(ordered, ordered[1:], strict=False)]
