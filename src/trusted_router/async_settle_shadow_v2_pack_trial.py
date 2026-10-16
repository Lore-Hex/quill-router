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
    derived = writer_population(files, record)
    expected = {(b['day'], b['boot']) for b in record['population']}
    require(len(expected) == len(record['population']) and {b for _, b in expected} == set(record['boots']),
            'TrialPopulation')
    counters = [retained_json(files.read(item['counter_ref'])) for item in plan['writer_operations']]
    require({(c['day'], c['boot']) for c in counters} == expected and len(counters) == len(expected),
            'TrialPopulation')
    population = {(b['day'], b['boot']): b for b in record['population']}
    for item, counter in zip(plan['writer_operations'], counters, strict=True):
        boot = population[counter['day'], counter['boot']]
        require(counter['region'] == boot['region'] and
            record['start_us'] <= item['start_us'] < item['end_us'] <= record['end_us'] and
            (item['start_us'], item['end_us']) == (boot['start_us'], boot['end_us']), 'TrialInterval')
        require(item['contender'] == any(s['contender'] for s in derived if s['boot'] == counter['boot']), 'TrialContender')
        for ref in item['operation_refs']:
            original = retained_json(files.read(ref))
            require(original['configuration'] == record['configuration'] and
                    original['resource'] == record['resource'] and original['region'] == boot['region'],
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
    stages = ('pre', 'post') if scope['scope'] == 'isolated_trial' else ('post',)
    for stage in stages:
        require(plan[stage]['interval'] == dict(start_us=record['start_us'], end_us=record['end_us']),
                'TrialInterval')
        segments = [s['fields'] for s in plan[stage]['load_segments']]
        require({(s['day'], s['boot']) for s in segments} == expected, 'TrialPopulation')
        for boot in record['boots']:
            lifetime = next(b for b in record['population'] if b['boot'] == boot)
            cursor = lifetime['start_us']
            for segment in sorted((s for s in segments if s['boot'] == boot), key=lambda s: s['start_us']):
                require(segment['start_us'] == cursor, 'TrialSegmentCoverage')
                cursor = segment['end_us']
            require(cursor == lifetime['end_us'], 'TrialSegmentCoverage')
        for segment in segments:
            lifetime = population[segment['day'], segment['boot']]
            require(segment['role'] == 'router' and segment['workspace_count'] == 1, 'TrialPopulation')
            matching = [s for s in derived if s['boot'] == segment['boot']
                and s['start_us'] <= segment['start_us'] < segment['end_us'] <= s['end_us']]
            require(len(matching) == 1 and segment['contender'] == matching[0]['contender'], 'TrialContender')
            require(segment['region'] == lifetime['region'] and segment['day'] == day_at(segment['start_us'])
                    and record['start_us'] <= segment['start_us'] < segment['end_us'] <= record['end_us'],
                    'TrialInterval')
    operation_population(files, plan, record)
    return {**record, 'segments': derived}


def native_peaks(files: PackFiles, plan: dict[str, Any], fields: dict[str, Any],
                 config: dict[str, Any], projection: dict[str, Any], cpu: dict[str, Any]) -> dict[str, Any]:
    """Compute peaks from timed original operations, never from peak projections."""
    start, end = fields['start_us'], fields['end_us']
    import json
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from trusted_router.async_settle_shadow_v2_pack_records import sorted_operations
    def originals() -> Any:
        for item in plan['writer_operations']:
            counter = retained_json(files.read(item['counter_ref']))
            if counter['boot'] == fields['boot']:
                for ref in item['operation_refs']:
                    raw = files.read(ref)
                    require(len(raw) <= 262144, 'OperationPageBound')
                    yield from retained_json(raw)['operations']
    bucket: Counter[str] = Counter()
    peaks: Counter[str] = Counter()
    with TemporaryDirectory(prefix='trial-operations-') as scratch:
        path = sorted_operations(originals(), Path(scratch))
        with path.open() as left_stream, path.open() as right_stream:
            right = map(json.loads, right_stream)
            pending = next(right, None)
            for operation in map(json.loads, left_stream):
                at = operation['at_us']
                while pending is not None and pending['at_us'] < at + 1000000:
                    for prefix, field in (('', 'logical_count'), ('bytes:', 'bytes'), ('rows:', 'rows')):
                        bucket[prefix + pending['operation']] += pending[field]
                    pending = next(right, None)
                if at < end and at + 1000000 > start:
                    for name, value in bucket.items():
                        peaks[name] = max(peaks[name], value)
                for prefix, field in (('', 'logical_count'), ('bytes:', 'bytes'), ('rows:', 'rows')):
                    bucket[prefix + operation['operation']] -= operation[field]
    def peak(name: str) -> int:
        return peaks[name]
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
    """Split at CPU samples and UTC within the independently compared role interval."""
    left, right = fields['start_us'], fields['end_us']
    boundaries = {left, right}
    sample = plan['trial']
    for at_ns in range(sample['start_ns'], sample['end_ns'], sample['interval_ns']):
        if left * 1000 < at_ns < right * 1000:
            require(at_ns % 1000 == 0, 'TrialSegmentBoundary')
            boundaries.add(at_ns // 1000)
    for at in range((left // 86400000000 + 1) * 86400000000, right, 86400000000):
        boundaries.add(at)
    ordered = sorted(boundaries)
    return [{**fields, 'start_us': start, 'end_us': end} for start, end in zip(ordered, ordered[1:], strict=False)]


def operation_population(files: PackFiles, plan: dict[str, Any], record: dict[str, Any]) -> None:
    """Compare lifecycle and pass operation identities before producing counts."""
    require('operation_population' in record, 'MissingOperationPopulation')
    expected = retained_json(files.read(record['operation_population']))
    require(expected['configuration'] == record['configuration'] and expected['complete'] is True,
            'OperationPopulation')
    from pathlib import Path
    from tempfile import TemporaryDirectory

    from trusted_router.async_settle_shadow_v2_pack_records import RecordSpool, same_records
    scratch = TemporaryDirectory(prefix='operation-population-')
    actual = RecordSpool(Path(scratch.name) / 'actual')
    export_actual = RecordSpool(Path(scratch.name) / 'exports')
    lifecycle_actual = RecordSpool(Path(scratch.name) / 'lifecycle')
    for item in plan['writer_operations']:
        counter = retained_json(files.read(item['counter_ref']))
        for ref in item['operation_refs']:
            for operation in retained_json(files.read(ref))['operations']:
                if operation['operation'] in ('startup', 'close'):
                    lifecycle_actual.append(dict(day=counter['day'], boot=counter['boot'], **operation))
                if operation['operation'] == 'export':
                    export_actual.append(operation)
                if operation['operation'] in ('startup', 'close', 'export'):
                    require(operation['logical_count'] > 0, 'OperationPopulation')
                    actual.append(dict(day=counter['day'], boot=counter['boot'],
                        id=operation['id'], operation=operation['operation'], at_us=operation['at_us']))
    require(same_records(actual, expected['operations']),
            'OperationPopulation')
    for boot in record['population']:
        selected = [o for o in expected['operations'] if (o['day'], o['boot']) == (boot['day'], boot['boot'])]
        require(any(o['operation'] == 'startup' and o['at_us'] == boot['start_us'] for o in selected),
                'StartupPopulation')
        require(any(o['operation'] == 'close' for o in selected), 'ClosePopulation')
    require('lifecycle_work' in record, 'MissingLifecycleWork')
    native = retained_json(files.read(record['lifecycle_work']))
    require(native['complete'] is True and native['platform'] == record['platform'] and
        native['shutdown'] == record['shutdown'], 'LifecyclePopulation')
    require(same_records(lifecycle_actual, native['operations']), 'LifecyclePopulation')
    for boot in record['population']:
        work = [o for o in native['operations'] if (o['day'], o['boot']) == (boot['day'], boot['boot'])]
        startups = [o for o in work if o['operation'] == 'startup']
        closes = [o for o in work if o['operation'] == 'close']
        require(len(startups) == 1 and startups[0]['at_us'] == boot['start_us'] and
            startups[0]['id'] == 'startup' and startups[0]['logical_count'] == 1, 'StartupPopulation')
        require(len(closes) == 2 and {o['id'] for o in closes} == {'close', 'result'} and
            all(o['logical_count'] == 1 and boot['start_us'] <= o['at_us'] < boot['end_us'] for o in closes), 'ClosePopulation')
    from trusted_router.async_settle_shadow_v2_pack_lifecycle import compare_lifecycle
    compare_lifecycle(files, record, native)
    from trusted_router.async_settle_shadow_v2_pack_work import export_work
    required_exports = export_work(files, plan)
    require(same_records(export_actual, required_exports), 'ExportPopulation')


def writer_population(files: PackFiles, record: dict[str, Any]) -> list[dict[str, Any]]:
    """Derive lifetimes and role boundaries from two independent originals."""
    require('platform' in record and 'roles' in record, 'MissingWriterPopulation')
    platform = retained_json(files.read(record['platform']))
    roles = retained_json(files.read(record['roles']))
    require(platform['complete'] is True and roles['complete'] is True and
        platform['resource'] == record['resource'] and roles['configuration'] == record['configuration'],
        'WriterPopulation')
    boots = {(b['day'], b['boot']): b for b in platform['boots']}
    require(len(boots) == len(platform['boots']) and set(boots) ==
        {(b['day'], b['boot']) for b in record['population']}, 'WriterPopulation')
    segments = []
    for declared in record['population']:
        boot = boots[declared['day'], declared['boot']]
        require(all(boot[k] == declared[k] for k in ('start_us', 'end_us', 'region')), 'WriterLifetime')
        intervals = sorted((r for r in roles['intervals'] if (r['day'], r['boot']) ==
            (boot['day'], boot['boot'])), key=lambda r: r['start_us'])
        cursor = boot['start_us']
        for interval in intervals:
            require(interval['start_us'] == cursor and cursor < interval['end_us'] <= boot['end_us'], 'WriterRoleCoverage')
            require(type(interval['contender']) is bool, 'WriterRole')
            cursor = interval['end_us']
            segments.append(dict(day=boot['day'], boot=boot['boot'], region=boot['region'],
                start_us=interval['start_us'], end_us=interval['end_us'], contender=interval['contender'],
                role='router', workspace_count=1))
        require(cursor == boot['end_us'], 'WriterRoleCoverage')
    require(len(roles['intervals']) == len(segments), 'WriterRolePopulation')
    return segments
