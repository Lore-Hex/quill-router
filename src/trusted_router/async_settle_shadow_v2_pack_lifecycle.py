"""Compare lifecycle work with platform clocks, Shutdown spans and native calls."""
from __future__ import annotations

from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_schema import require


def compare_lifecycle(files: Files, record: dict[str, Any], work: dict[str, Any]) -> None:
    """Missing original calls or clock mappings leave the measurement unavailable."""
    require('lifecycle_calls' in record, 'MissingLifecycleCalls')
    original = retained_json(files.read(record['lifecycle_calls']))
    require(original['complete'] is True and original['platform'] == record['platform'], 'LifecycleCalls')
    platform = retained_json(files.read(record['platform']))
    boots = {(b['day'], b['boot']): b for b in platform['boots']}
    shutdowns = {}
    for item in record['shutdown']:
        span = retained_json(files.read(item['original']))
        shutdowns[span['day'], span['boot']] = (item['original'], span)
    phases = {(p['day'], p['boot'], p['id']): p for p in original['phases']}
    require(len(phases) == len(original['phases']) == len(work['operations']), 'LifecycleCalls')
    used = set()
    for operation in work['operations']:
        key = operation['day'], operation['boot'], operation['id']
        require(key in phases and key not in used, 'LifecycleCalls')
        used.add(key)
        phase = phases[key]
        boot = boots[key[:2]]
        require('clock_origin' in boot and 'startup_span' in boot, 'LifecycleClock')
        origin = boot['clock_origin']
        require(origin['boot'] == boot['boot'] and type(origin['monotonic_at_boot_us']) is int,
                'LifecycleClock')
        if operation['id'] == 'startup':
            expected = boot['startup_span']
            require(phase['source'] == record['platform'] and expected[0] == origin['monotonic_at_boot_us'],
                    'LifecycleSpan')
        else:
            ref, span = shutdowns[key[:2]]
            require(span.get('clock') == 'monotonic_us' and span.get('clock_origin') == origin,
                    'LifecycleClock')
            require(phase['source'] == ref, 'LifecycleSpan')
            expected = span['spans'][operation['id']]
        left, right = phase['span']
        require([left, right] == expected, 'LifecycleSpan')
        require(type(left) is int and type(right) is int and 0 <= left <= right, 'LifecycleSpan')
        at = boot['start_us'] + left - origin['monotonic_at_boot_us']
        require(operation['at_us'] == at and operation['elapsed_us'] == right - left and
            boot['start_us'] <= at < boot['end_us'] and at + right - left <= boot['end_us'], 'LifecycleTime')
        calls = phase['calls']
        require(bool(calls) and len({c['id'] for c in calls}) == len(calls), 'LifecycleCalls')
        totals = dict.fromkeys(('rows', 'bytes', 'native_rpcs'), 0)
        for call in calls:
            require(call['phase'] == operation['id'] and left <= call['span'][0] <= call['span'][1] <= right,
                    'LifecycleCallSpan')
            for field in totals:
                require(type(call[field]) is int and call[field] >= 0, 'LifecycleCost')
                totals[field] += call[field]
        require(all(operation[field] == value for field, value in totals.items()), 'LifecycleCost')
    require(used == set(phases), 'LifecycleCalls')
