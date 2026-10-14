"""Exact export work populations from compared passes and original receipts."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import Files, approval
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_schema import require, sha256


def receipt_work(value: dict[str, Any]) -> dict[str, Any]:
    return dict(id=f"export:{value['pass']}:{value['operation']}:{value['object']['path']}",
        operation='export', at_us=value['at_us'], logical_count=1, rows=0,
        bytes=value['bytes'], native_rpcs=value['native_rpcs'], elapsed_us=value['elapsed_us'], slot=None)


def export_work(files: Files, plan: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """A timing capture supplies times only; compared read records supply costs."""
    planned = retained_json(files.read(plan['output_plan']))
    approval(files, plan['output_plan_review'], plan['output_plan'])
    receipts = [retained_json(files.read(ref)) for ref in plan['output_receipts']]
    expected = {(p['pass'], o['operation'], o['object']['path']): (p, o)
        for p in planned['passes'] for o in p['outputs']}
    require(len(expected) == sum(len(p['outputs']) for p in planned['passes']), 'OutputPopulation')
    require(len(receipts) == len(expected), 'ExportPopulation')
    seen = set()
    for receipt in receipts:
        key = receipt['pass'], receipt['operation'], receipt['object']['path']
        require(key in expected and key not in seen, 'ExportPopulation')
        seen.add(key)
        passed, output = expected[key]
        require(receipt['object'] == output['object'] and passed['start_us'] <= receipt['at_us'] < passed['end_us'] and
            receipt['at_us'] + receipt['elapsed_us'] <= passed['end_us'], 'ExportPopulation')
        raw = files.read(receipt['object'])
        require(receipt['bytes'] == (0 if receipt['operation'] == 'delete' else len(raw)), 'ExportPopulation')
        yield receipt_work(receipt)
    roots = plan.get('export_accounting', {}).get('roots', [])
    require('accounting_roots' in planned and planned['accounting_roots'] == roots, 'ExportPassPopulation')
    if roots:
        require('export_timing' in plan, 'ExportTimingPopulation')
        timing = retained_json(files.read(plan['export_timing']))
        approval(files, plan['export_timing_review'], plan['export_timing'])
        require(timing['complete'] is True and timing['roots'] == roots, 'ExportTimingPopulation')
        times = {t['id']: t for t in timing['calls']}
        require(len(times) == len(timing['calls']), 'ExportTimingPopulation')
        used = set()
        require(len(roots) == len(planned['passes']), 'OutputPassPopulation')
        for root_ref, passed in zip(roots, planned['passes'], strict=True):
            root = retained_json(files.read(root_ref))
            prefix = root_ref['path'].rsplit('/', 1)[0] + '/'
            for ref in root['details']['reads']:
                page = retained_json(files.read({**ref, 'path': prefix + ref['path']}))
                for index, call in enumerate(page['entries']):
                    identity = f"read:{root_ref['sha256']}:{page['sequence']}:{index}"
                    require(identity in times, 'ExportTimingPopulation')
                    timed = times[identity]
                    require(timed['original_sha256'] == sha256(call) and
                        passed['start_us'] <= timed['at_us'] < passed['end_us'] and
                        timed['at_us'] + call['elapsed_us'] <= passed['end_us'], 'ExportTimingPopulation')
                    used.add(identity)
                    yield dict(id=identity, operation='export', at_us=timed['at_us'],
                        logical_count=1, rows=call['count'], bytes=call['body_bytes'],
                        native_rpcs=call['native_rpcs'], elapsed_us=call['elapsed_us'], slot=None)
        require(used == set(times), 'ExportTimingPopulation')
