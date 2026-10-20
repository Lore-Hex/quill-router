"""Fake timed export calls joined to real exporter read records."""
from __future__ import annotations

import json

from tests.test_async_settle_shadow_v2_pack import review
from trusted_router.async_settle_shadow_v2_schema import sha256


def timed_export(files, budget, passed):
    root_ref = passed['accounting']
    root = json.loads(files.read(root_ref))
    interval = budget['post']['interval']
    calls = []
    work = []
    for ref in root['details']['reads']:
        page = json.loads(files.read({**ref, 'path': passed['prefix'] + ref['path']}))
        for index, call in enumerate(page['entries']):
            calls.append(dict(id=f"read:{root_ref['sha256']}:{page['sequence']}:{index}",
                original_sha256=sha256(call), at_us=interval['end_us'] - max(1, call['elapsed_us'])))
            work.append(dict(id=calls[-1]['id'], operation='export', at_us=calls[-1]['at_us'],
                logical_count=1, rows=call['count'], bytes=call['body_bytes'], native_rpcs=call['native_rpcs'],
                elapsed_us=call['elapsed_us'], slot=None))
    budget['export_timing'] = files.obj('trial/export-timing', dict(complete=True, roots=[root_ref], calls=calls))
    budget['export_timing_review'] = review(files, budget['export_timing'], 'reviews/export-timing')
    budget['export_accounting'] = dict(roots=[root_ref])
    output = json.loads(files.read(budget['output_plan']))
    output['accounting_roots'] = [root_ref]
    budget['output_plan'] = files.obj('trial/compared-output-plan', output)
    budget['output_plan_review'] = review(files, budget['output_plan'], 'reviews/compared-output-plan')
    for ref in budget['output_receipts']:
        receipt = json.loads(files.read(ref))
        work.append(dict(id=f"export:{receipt['pass']}:{receipt['operation']}:{receipt['object']['path']}",
            operation='export', at_us=receipt['at_us'], logical_count=1, rows=0, bytes=receipt['bytes'],
            native_rpcs=receipt['native_rpcs'], elapsed_us=receipt['elapsed_us'], slot=None))
    item = budget['writer_operations'][0]
    original = json.loads(files.read(item['operation_refs'][0]))
    original['operations'] = [o for o in original['operations'] if o['operation'] != 'export'] + work
    item['operation_refs'] = [files.obj('trial/composed-operations', original)]
    record = json.loads(files.read(budget['trial_record']))
    population = json.loads(files.read(record['operation_population']))
    population['operations'] = [o for o in population['operations'] if o['operation'] != 'export'] + [dict(
        day=original['day'], boot=original['boot'], **{k: o[k] for k in ('id', 'operation', 'at_us')}) for o in work]
    record['operation_population'] = files.obj('trial/composed-operation-population', population)
    budget['trial_record'] = files.obj('trial/timed-record', record)
    budget['trial_review'] = review(files, budget['trial_record'], 'reviews/timed-record')
    # All fake calls occur together; expected sums are independent of the peak implementation.
    values = dict(export_operations_1s=len(work),
        export_operations_per_second=dict(numerator=len(work), denominator=1),
        export_bytes_per_second=dict(numerator=sum(o['bytes'] for o in work), denominator=1))
    limits = json.loads(files.read(budget['pre']['limits']))
    # One reviewed configuration persists across real captures with different byte totals.
    approved_values = dict(export_operations_1s=1000,
        export_operations_per_second=dict(numerator=1000, denominator=1),
        export_bytes_per_second=dict(numerator=1000000, denominator=1))
    limits['fleet'].update(approved_values)
    for region in limits['regions']:
        region['approved'].update(approved_values)
    approved = files.obj('trial/export-limits', limits)
    approved_review = review(files, approved, 'reviews/export-limits')
    for stage in ('pre', 'post'):
        budget[stage]['limits'] = approved
        budget[stage]['limits_review'] = approved_review
        for key, value in values.items():
            budget[stage]['load_segments'][0]['derivations'][key] = dict(kind='measured',
                original=files.obj('trial/export-peaks/' + key, dict(value=value)), field=['value'])


def rebuild_referring_records(files, spec):
    """Rebuild changed original references and reviews, preserving unrelated bytes."""
    import hashlib

    from trusted_router.detached_jws import canonical
    changes = {}
    def digest(raw):
        return hashlib.sha256(raw).hexdigest()
    for ref in spec['files']:
        path = files.root / ref['path']
        if path.is_file():
            raw = path.read_bytes()
            if digest(raw) != ref['sha256']:
                changes[ref['sha256']] = dict(sha256=digest(raw), bytes=len(raw))
    def updated(value):
        if isinstance(value, str):
            seen = set()
            while value in changes:
                assert value not in seen
                seen.add(value)
                value = changes[value]['sha256']
            return value
        if isinstance(value, list):
            return [updated(v) for v in value]
        if isinstance(value, dict):
            result = {k: updated(v) for k, v in value.items()}
            if set(value) == {'path', 'sha256', 'bytes'} and value['sha256'] in changes:
                current = value['sha256']
                while current in changes:
                    result['bytes'] = changes[current]['bytes']
                    current = changes[current]['sha256']
            return result
        return value
    for _ in range(64):
        changed = False
        for ref in spec['files']:
            path = files.root / ref['path']
            raw = path.read_bytes()
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            new = updated(value)
            if new != value:
                body = canonical(new)
                changes[digest(raw)] = dict(sha256=digest(body), bytes=len(body))
                path.write_bytes(body)
                changed = True
        if not changed:
            revised = updated(spec)
            spec.clear()
            spec.update(revised)
            return
    raise AssertionError('referring records did not settle')
