"""Explicit fake native call originals, separate from the measured work lists."""
from __future__ import annotations

import json

from tests.test_async_settle_shadow_v2_pack import review
from trusted_router.async_settle_shadow_v2_schema import sha256


def captured_object(files, name, value):
    return files.obj(name + "/" + sha256(value), value)


def lifecycle_capture(files, plan, record):
    """Supply fake platform clock and call records; preserve the Shutdown originals."""
    platform = json.loads(files.read(record['platform']))
    phases = []
    work = []
    for boot in platform['boots']:
        item = next(s for s in plan['shutdown'] if json.loads(files.read(s['original']))['boot'] == boot['boot'])
        span = json.loads(files.read(item['original']))
        boot['clock_origin'] = span['clock_origin']
        origin = boot['clock_origin']['monotonic_at_boot_us']
        boot['startup_span'] = [origin, origin + 1]
    record['platform'] = captured_object(files, 'trial/call-platform', platform)
    for boot in platform['boots']:
        item = next(s for s in plan['shutdown'] if json.loads(files.read(s['original']))['boot'] == boot['boot'])
        original = json.loads(files.read(item['original']))
        for name, rows, size, rpcs in (('startup', 0, 0, 1), ('close', 6, 600, 3), ('result', 1, 100, 2)):
            span = boot['startup_span'] if name == 'startup' else original['spans'][name]
            # These are declared fake native call costs, not inferred transaction counts.
            phases.append(dict(day=boot['day'], boot=boot['boot'], id=name, span=span,
                source=record['platform'] if name == 'startup' else item['original'],
                calls=[dict(id=name + '-native', phase=name, span=span, rows=rows, bytes=size, native_rpcs=rpcs)]))
            work.append(dict(day=boot['day'], boot=boot['boot'], id=name,
                operation='startup' if name == 'startup' else 'close', logical_count=1, slot=None,
                at_us=boot['start_us'] + span[0] - boot['clock_origin']['monotonic_at_boot_us'],
                elapsed_us=span[1] - span[0], rows=rows, bytes=size, native_rpcs=rpcs))
    record['lifecycle_calls'] = captured_object(files, 'trial/lifecycle-calls', dict(complete=True,
        platform=record['platform'], phases=phases))
    for item in plan['writer_operations']:
        value = json.loads(files.read(item['operation_refs'][0]))
        value['operations'] = [o for o in value['operations'] if o['operation'] not in ('startup', 'close')] + [
            {k: v for k, v in o.items() if k not in ('day', 'boot')} for o in work if o['boot'] == value['boot']]
        item['operation_refs'] = [captured_object(files, 'trial/call-operations', value)]
    record['lifecycle_work'] = captured_object(files, 'trial/call-work', dict(complete=True, platform=record['platform'],
        shutdown=record['shutdown'], operations=work))
    refresh_population(files, plan, record)
    return record


def refresh_population(files, plan, record):
    operations = []
    for item in plan['writer_operations']:
        value = json.loads(files.read(item['operation_refs'][0]))
        operations.extend(dict(day=value['day'], boot=value['boot'],
            **{k: o[k] for k in ('id', 'operation', 'at_us')}) for o in value['operations']
            if o['operation'] in ('startup', 'close', 'export'))
    record['operation_population'] = captured_object(files, 'trial/call-population', dict(
        configuration=record['configuration'], complete=True, operations=operations))
    plan['trial_record'] = captured_object(files, 'trial/call-record', record)
    plan['trial_review'] = review(files, plan['trial_record'], 'trial/call-review/' + plan['trial_record']['sha256'])
