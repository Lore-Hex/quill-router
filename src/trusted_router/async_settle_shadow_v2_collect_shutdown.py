"""Measured shutdown spans, with no zero substitution for missing observations."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_collect_inventory import InventoryResult
from trusted_router.async_settle_shadow_v2_collect_ports import (
    TAGS,
    LifecyclePort,
    Pass,
    Query,
    approval,
    classified_records,
)
from trusted_router.async_settle_shadow_v2_gate_inputs import SHUTDOWN_SOURCE
from trusted_router.async_settle_shadow_v2_schema import require


@dataclass(frozen=True)
class ShutdownResult:
    envelope: dict[str, Any] | None
    measurements: dict[str, Any]
    entries: tuple[dict[str, Any], ...]
    complete: bool


def durations(record: dict[str, Any]) -> dict[str, int]:
    """Disjoint preparation/wait precede final pass; close/result nest in pass.

    Record absolute monotonic microseconds, never subtract wall-clock timestamps.
    A3's local total is preparation + wait + final pass; never add close/result
    a second time. Both transactions must be contained in the final pass.
    """
    spans = record['spans']
    required = ('preparation', 'export_wait', 'final_pass', 'close', 'result')
    require(set(spans) == set(required))
    for span in spans.values():
        require(type(span) is list and len(span) == 2)
        require(all(type(t) is int and t >= 0 for t in span) and span[0] <= span[1])
    prep, wait, final, close, result = (spans[key] for key in required)
    require(prep[1] == wait[0] and wait[1] == final[0])
    require(final[0] <= close[0] <= close[1] <= result[0] <= result[1] <= final[1])
    return {key + '_us': spans[key][1] - spans[key][0] for key in required}


def collect(run: Pass, *, inventory: InventoryResult, logs: LifecyclePort, query: Query,
            originals: dict[str, dict[str, dict[str, Any]]],
            review: dict[str, Any] | None = None) -> ShutdownResult:
    records = list(run.scan(logs, query))
    records = classified_records(run, records, 'shutdown')
    entries = []
    missing = []
    if not inventory.complete:
        run.gap('inventory', 'independent_inventory_incomplete', 'writer_closure_gap')
    for boot in inventory.boots:
        if boot['role'] != 'router':
            continue
        selected = [r for r in records if r.get('kind') == 'a3_shutdown_span' and r.get('boot') == boot['boot']]
        key = query.day + '/' + boot['boot']
        try:
            require(len(selected) == 1 and boot['grace_us'] is not None)
            record = selected[0]
            require(all(record[k] == boot[k] for k in ('instance', 'launch', 'revision', 'region')))
            require(record['day'] == query.day and record['interval_end_us'] == boot['end_us'])
            require(record['clock'] == 'monotonic_us' and record['complete'] is True)
            measured = durations(record)
            require(record['clock_origin']['monotonic_at_boot_us'] <= record['spans']['preparation'][0])
            outcome = 'hard_kill' if boot['reason'] == 'hard_kill' else 'graceful'
            require(record['outcome'] == outcome)
            if outcome == 'hard_kill':
                run.gap(key, 'hard_kill', 'writer_closure_gap')
            total = measured['preparation_us'] + measured['export_wait_us'] + measured['final_pass_us']
            if (total > min(boot['grace_us'], 9_000_000) or measured['final_pass_us'] > 4_000_000
                    or measured['close_us'] > 2_000_000 or measured['result_us'] > 2_000_000):
                run.gap(key, 'combined_grace_or_pass_exceeded', 'close_handoff_budget_gap')
            source = dict(**TAGS, day=query.day, boot=boot['boot'], grace_us=boot['grace_us'],
                          outcome=outcome, **measured)
            source_ref = run.files.obj(f'sources/measurement/{key}.json', source, SHUTDOWN_SOURCE)
            measurement = dict(**source, source=source_ref)
            measurement_ref = run.files.obj(f'shutdown/{key}/measurement.json', measurement, a2.SHUTDOWN_MEASUREMENT)
            refs: dict[str, Any] = {}
            decoded: dict[str, Any] = {}
            for field, schema in (('original_close', a1.CLOSURE), ('close_result', a1.CLOSE_RESULT)):
                ref = originals.get(key, {}).get(field)
                if ref is not None:
                    value = schema.parse(run.files.read(ref))
                    require(value['boot'] == boot['boot'] and value['day'] == query.day)
                    require(value['router_revision'] == boot['revision'])
                    decoded[field] = value
                else:
                    run.gap(key, field + '_missing', 'writer_closure_gap')
                refs[field] = ref
            if set(decoded) == {'original_close', 'close_result'}:
                a1.validate_close_result(decoded['close_result'], decoded['original_close'],
                                         termination_applies=boot['reason'] != 'utc_boundary')
                require(decoded['original_close']['interval_end_us'] == boot['end_us'])
                require(decoded['original_close']['region'] == boot['region'])
                require(decoded['close_result']['elapsed_us'] == measured['close_us'])
            entry = dict(day=query.day, boot=boot['boot'], termination_applies=boot['reason'] != 'utc_boundary',
                reason=boot['reason'], interval_end_us=boot['end_us'], grace_us=boot['grace_us'],
                measurement=measurement_ref, **refs)
            a2.SHUTDOWN_ENTRY(entry)
            entries.append(entry)
            run.files.obj(f'shutdown/{key}/span-original.json', record)
        except (KeyError, ValueError, OSError, TypeError):
            run.gap(key, 'missing_or_invalid_span_or_original', 'close_handoff_budget_gap')
            # Frozen measurement has no nullable durations. Keep expected key
            # missing; never manufacture a schema-valid measurement of zero.
            missing.append(dict(day=query.day, boot=boot['boot'], measurement=None))
    entries.sort(key=lambda r: (r['day'], r['boot']))
    detail = run.files.detail('shutdown/writers', entries, 'shutdown_writers')
    measured_ref = run.files.obj('shutdown/measurements.json', [r['measurement'] for r in entries])
    run.files.obj('shutdown/expected.json', dict(expected=[dict(day=query.day, boot=b['boot'])
        for b in inventory.boots if b['role'] == 'router'], unmatched=list(inventory.expected),
        missing=missing, sources=run.sources, gaps=run.missing))
    approved = approval(run.files, review, measured_ref)
    envelope = None
    if approved is not None:
        value = dict(**TAGS, writers=detail, measurements=measured_ref, approval=approved,
                     complete=not missing and inventory.complete and not any(
                         g['gap'] == 'artifact_gap' for g in run.missing))
        # complete describes enumeration; a measured hard kill can be complete
        # while remaining BLOCKED, and the gate checks its actual outcome.
        envelope = run.files.obj('shutdown/inventory.json', value, a2.SHUTDOWN_INVENTORY)
    return ShutdownResult(envelope, measured_ref, tuple(entries), not run.missing)
