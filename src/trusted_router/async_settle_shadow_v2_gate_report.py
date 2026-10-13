"""Build the A2 report and complete writer/region detail from evaluated evidence."""
from __future__ import annotations

from fractions import Fraction
from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_a2_contract import REQUIRED_RULES
from trusted_router.async_settle_shadow_v2_gate_failures import (
    TAG,
    detail,
    listing,
    writer_subjects,
)
from trusted_router.async_settle_shadow_v2_gate_metrics import counts, observations, total
from trusted_router.async_settle_shadow_v2_gate_preimages import input_inventory
from trusted_router.async_settle_shadow_v2_gate_result import Failure, Peak, Report, Subject
from trusted_router.async_settle_shadow_v2_schema import require

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context


def peaks(value: Peak | None) -> dict[str, Any]:
    result = dict.fromkeys(a2.PEAKS.fields)
    if value is not None:
        result.update(simultaneous_routers=value.simultaneous, simultaneous_publishers=value.publisher_simultaneous)
        for key, number in (('reads_per_second', value.reads), ('pending_rows_per_second', value.pending),
                            ('trust_rows_per_second', value.trust), ('health_rows_per_second', value.health)):
            fraction = Fraction(str(number))
            result[key] = dict(numerator=fraction.numerator, denominator=fraction.denominator)
    return result


def population(ctx: Context, window: dict[str, int], required: tuple[str, ...]) -> dict[str, Any]:
    left, right = window['start_us'], window['end_us']
    all_events = ctx.events()
    expected = {(v['day'], v['boot']) for v in writer_subjects(ctx, Subject()) if v['role'] == 'router'}
    counters = ctx.kind(p1.KINDS[3])
    known = expected == {(c['day'], c['boot']) for c in counters} and ctx.bundle.inventory is not None
    terminals = [e for _, e in all_events if e['phase'] in ('settle', 'refund') and left <= e['event_at_us'] < right]
    terminal_complete = known and all(len([e for p, e in all_events if (p['day'], p['boot']) == (c['day'], c['boot']) and e['phase'] in ('settle', 'refund')]) == c['settle_attempts'] + c['refund_attempts'] for c in counters)
    all_samples = ctx.kind(p1.KINDS[0])
    samples = [s for s in all_samples if left <= s['observed_at_us'] < right]
    # SAMPLE identity is authorization_day/id, already unique at the key boundary.
    available_samples = terminal_complete and len([s for s in all_samples if (s['original_terminal_receipt']['day'], s['original_terminal_receipt']['boot']) in expected]) == sum(c['samples_inserted'] for c in counters)
    references = {(e['authorization_day'], e['authorization_id']) for e in terminals if e['partition'] == 'A'}
    available_samples &= references <= {(s['authorization_day'], s['authorization_id']) for s in all_samples}
    slots = [e for page in ctx.attached('publisher_trace') for e in page['entries'] if left <= e['slot'] * 2000000 < right]
    available_slots = known and not any(i.gap == 'publisher_trace_gap' for i in ctx.issues)
    shutdown = [v for v in ctx.attached('shutdown_entry') if left < v['interval_end_us'] <= right]
    expected_closes = {(c['day'], c['boot']) for c in counters if left < c['flushed_at_us'] <= right}
    known_closes = known and expected_closes == {(s['day'], s['boot']) for s in shutdown}
    closes = [v for v in ctx.kind(a1.KINDS[-1]) if (v['day'], v['boot']) in expected_closes]
    known_results = known_closes and {(v['day'], v['boot']) for v in closes} == expected_closes
    measurements = [v for v in ctx.attached('shutdown_measurement') if (v['day'], v['boot']) in expected_closes]
    known_measurements = known_closes and {(v['day'], v['boot']) for v in measurements} == expected_closes
    term = {(s['day'], s['boot']) for s in shutdown if s['termination_applies']}
    for available, rule, gap in ((available_samples, 'G5', 'eligible_coverage_gap'),
        (terminal_complete, 'G5', 'terminal_coverage_gap'), (available_slots, 'G6', 'publisher_trace_gap'),
        (known_results, 'G8', 'writer_closure_gap'), (known_measurements, 'G8', 'close_handoff_budget_gap')):
        if rule in required and not available:
            ctx.issue(gap, 'required metric population unavailable')
            ctx.issue('artifact_gap', 'required metric source unavailable')
    return dict(samples=samples if available_samples and 'G5' in required else None,
        terminals=terminals if terminal_complete and 'G5' in required else None,
        slots=slots if available_slots and 'G6' in required else None,
        closes=closes if known_results and 'G8' in required else None,
        handoffs=measurements if known_measurements and 'G8' in required else None,
        terminations=[v for v in measurements if (v['day'], v['boot']) in term] if known_measurements and 'G8' in required else None)


def window_for(ctx: Context) -> dict[str, int]:
    if ctx.bundle.window is not None:
        value = dict(ctx.bundle.window)
        p1.INTERVAL(value)
        return value
    intervals = [v['lifetime'] for m in ctx.kind(p1.KINDS[5]) if 'router_writers' in m
                 for v in m['router_writers'].values()]
    require(bool(intervals), 'artifact_gap: observation window unavailable')
    return dict(start_us=min(v['start_us'] for v in intervals), end_us=max(v['end_us'] for v in intervals))


def build(ctx: Context, failures: tuple[Failure, ...], legacy: Report, window: dict[str, int],
          populations: dict[str, Any], verifiers: dict[str, Any]) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], dict[str, bytes]]:
    required = REQUIRED_RULES[ctx.gate]
    all_counters = ctx.kind(p1.KINDS[3])
    events = [ev for _, ev in ctx.events()]
    subjects = writer_subjects(ctx, Subject())
    expected = {(s['day'], s['boot']) for s in subjects if s['role'] == 'router'}
    available = expected == {(c['day'], c['boot']) for c in all_counters} and ctx.bundle.inventory is not None
    aggregate = counts(ctx, all_counters if available else None, events if available else None, required)
    writers = []
    for sub in subjects:
        day, role, boot = (sub[k] for k in ('day', 'role', 'boot'))
        inventoried = next((b for b in ctx.bundle.inventory.boots if b.boot == boot and b.role == role), None) if ctx.bundle.inventory else None
        if inventoried is None:
            # Missing authoritative revision/interval cannot produce a WRITER_REPORT.
            continue
        own = [c for c in all_counters if (c['day'], c['boot']) == (day, boot)]
        own_events = [ev for page, ev in ctx.events() if (page['day'], page['boot']) == (day, boot)]
        manifest = next((m for m in ctx.kind(p1.KINDS[5]) if m.get('day') == day and 'router_writers' in m), None)
        interval = manifest[role + '_writers'].get(boot, {}).get('lifetime') if manifest else None
        if interval is None:
            continue
        metrics = counts(ctx, own if own else None, own_events if own else None, required, enclave=role == 'enclave')
        writers.append(dict(**TAG, day=day, boot=boot, role=role, revision=inventoried.revision,
            region=inventoried.region, interval=interval, **metrics,
            first_seed_at_us=legacy.first_seed_us if any(s['deployment']['instance'] == boot and s['observed_at_us'] == legacy.first_seed_us for s in populations['samples'] or []) else None,
            first_reset_at_us=None, first_gap_at_us=None))
    by_boot = {b.boot: b.region for b in ctx.bundle.inventory.boots} if ctx.bundle.inventory else {}
    regions = []
    for region in sorted(set(by_boot.values())):
        selected = {}
        for pop, values in populations.items():
            def actual(value: dict[str, Any], pop: str = pop) -> str | None:
                if pop == 'samples':
                    return by_boot.get(value['deployment']['instance'])
                if pop == 'terminals':
                    return next((by_boot.get(page['boot']) for page, ev in ctx.events() if ev is value), None)
                return by_boot.get(value['boot'])
            selected[pop] = None if values is None or any(actual(v) is None for v in values) else [v for v in values if actual(v) == region]
        observed = observations(**selected)
        regions.append(dict(**TAG, region=region, writer_days=sum(w['region'] == region for w in writers),
            peaks=peaks(next((p for p in legacy.peaks or () if p.region == region), None)), timing=observed['timing'],
            seed_at_us=min((s['observed_at_us'] for s in populations['samples'] or [] if s['deployment']['region'] == region and s['authorization_day'] + '/' + s['authorization_id'] in ctx.verified), default=None)))
    tallies, listed = listing(failures, ctx.bundle.days)
    inputs, leaves = input_inventory(ctx)
    streams = dict(writers=writers, regions=regions, failures=listed, input_inventory=inputs, reset_history=[])
    details = {}
    pages = {}
    for stream, entries in streams.items():
        details[stream], pages[stream] = detail(entries, stream)
    publisher: dict[str, Any] = {field: total(c['publisher'][field] for c in all_counters) if available and 'G6' in required else None
                 for field in a2.PUBLISHER.fields if field != 'peaks'}
    publisher['peaks'] = peaks(None)
    handoffs = ctx.attached('handoff_accounting')
    handoff = {field: total(int(h['expected_handoffs' if field == 'expected' else field]) for h in handoffs) if available and 'G8' in required and len(handoffs) == len(expected) else None
               for field in a2.HANDOFF.fields}
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    require(ctx.bundle.proof is not None and ctx.proof is not None, 'artifact_gap')
    assert ctx.bundle.proof is not None and ctx.proof is not None
    output = dict(**TAG, gate=ctx.gate, completed_at_us=ctx.bundle.completed_at_us,
        proof_manifest_sha256=file_digest(ctx.bundle.proof), prerequisite_ledger_root=ctx.proof['ledger_root'],
        days=list(ctx.bundle.days), window=window,
        rules={f'G{i}': 'not_required' if f'G{i}' not in required else 'fail' if any(f.rule == f'G{i}' for f in failures) else 'pass' for i in range(1, 12)},
        **aggregate, handoff=handoff, publisher=publisher,
        fleet_peaks=peaks(next((p for p in legacy.peaks or () if p.region is None), None)), **observations(**populations),
        first_seed_at_us=legacy.first_seed_us, first_reset_at_us=min(legacy.reset_timestamps_us, default=None),
        first_gap_at_us=min(legacy.gap_timestamps_us, default=None), clock_start_us=legacy.clock_start_us,
        elapsed_seconds=None if legacy.clock_start_us is None else (window['end_us'] - legacy.clock_start_us) // 1000000,
        failure_counts=tallies, **details, verifiers=verifiers,
        complete=not any(i.gap in ('artifact_gap', 'evidence_schema_gap') for i in ctx.issues))
    # Fleet and region series are independent recomputations of the same union.
    for timing, value in output['timing'].items():
        if regions and all(region['timing'][timing]['count'] is not None for region in regions):
            assert value['count'] == total(region['timing'][timing]['count'] for region in regions)
            assert value['null_count'] == total(region['timing'][timing]['null_count'] for region in regions)
    a2.GATE_REPORT(output)
    return output, pages, leaves
