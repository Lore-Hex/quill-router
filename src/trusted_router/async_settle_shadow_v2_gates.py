"""Offline shared P5 gates. Internal results are lossless; publication awaits A2."""
from __future__ import annotations

import datetime as dt
import hashlib
from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router import async_settle_shadow_v2_gate_report as a2report
from trusted_router.async_settle_shadow_v2_a2_contract import PASS_AGGREGATES, REQUIRED_RULES
from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
from trusted_router.async_settle_shadow_v2_gate_accounting import (
    PREREQUISITES,
    authorization,
    deployment,
    durability,
    receipt_accounting,
)
from trusted_router.async_settle_shadow_v2_gate_bundle import (
    Context,
    coverage_issues,
    decode_bundle,
    verify_samples,
)
from trusted_router.async_settle_shadow_v2_gate_failures import collect as collect_failures
from trusted_router.async_settle_shadow_v2_gate_failures import normalize
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Ports
from trusted_router.async_settle_shadow_v2_gate_load import load_budgets, observer_health
from trusted_router.async_settle_shadow_v2_gate_result import (
    Metric,
    Peak,
    Report,
    Result,
    Subject,
    WriterReport,
)
from trusted_router.async_settle_shadow_v2_gate_verifiers import (
    check_calls,
    invoke_retained,
    records,
)
from trusted_router.async_settle_shadow_v2_schema import day_at, require

STAGES = REQUIRED_RULES


def names(rule: str) -> tuple[str, ...]:
    return tuple(row.name for row in a2.FAILURE_RULES if row.rule == rule)


def resets(ctx: Context) -> tuple[int, ...]:
    support = ctx.bundle.reset_support
    if support is None or ctx.ports is None:
        ctx.issue('missing_support', 'retained reset support or validator absent')
        return ()
    history = [dict(r) for r in support.resets]
    for sample in ctx.kind(p1.KINDS[0]):
        if sample['classification'] in names('G9'):
            history.append(dict(at_us=sample['observed_at_us'], revision=sample['deployment']['router_revision'], reason=sample['classification']))
    for counter in ctx.kind(p1.KINDS[3]):
        if counter['diagnostics'].get('exclusion_conflict', 0):
            history.append(dict(at_us=counter['flushed_at_us'], revision=counter['router_revision'], reason='exclusion_conflict'))
        if counter['last_mismatch_at_us'] is not None or counter['conflicting_samples']:
            history.append(dict(at_us=counter['last_mismatch_at_us'], revision=counter['router_revision'], reason='counter_mismatch'))
    try:
        # The retained function owns resolution schema, rollback partial order,
        # provenance support and persistent missing-support behavior.
        legacy = invoke_retained(ctx, 'report', list(support.rows), list(ctx.bundle.days), support.proof)
        for gap in legacy['gaps']:
            if gap.endswith('missing_support'):
                ctx.issue('missing_support', 'retained report: ' + gap)
        history.extend(legacy['resets'])
        serving = [(b.start_us, b.end_us or ctx.bundle.completed_at_us, b.revision)
                   for b in ctx.bundle.inventory.boots] if ctx.bundle.inventory else []
        manifests = {m['day']: {'router_revisions': m['revisions']['router']} for m in ctx.kind(p1.KINDS[5]) if 'revisions' in m}
        invoke_retained(ctx, 'collect_rollbacks', history, serving, support.proof.get('resolved_mismatches', []), manifests)
    except (ValueError, KeyError, TypeError):
        ctx.issue('missing_support', 'invalid retained reset/resolution evidence')
    from trusted_router.async_settle_shadow_v2_gate_resets import normalized, supported
    from trusted_router.detached_jws import canonical
    entries: dict[bytes, dict[str, Any]] = {}
    for reset in history:
        try:
            entry = normalized(reset)
        except (ValueError, KeyError, TypeError):
            if reset.get('reason') in names('G9'):
                ctx.issue(reset['reason'], 'reset assertion identity unavailable', Subject(wire=a2.N()), unavailable=True)
            ctx.issue('missing_support', 'reset identity cannot be normalized')
            ctx.issue('artifact_gap', 'reset history identity unavailable')
            continue
        wire = a2.Q(entry)
        identity = canonical(wire)
        if identity in entries and entries[identity] != entry:
            ctx.issue('missing_support', 'conflicting reset resolution records', Subject(wire=wire))
            ctx.issue('artifact_gap', 'reset history conflict')
            continue
        entries[identity] = entry
        if not supported(ctx, entry):
            ctx.issue('missing_support', 'reset provenance unavailable', Subject(wire=wire))
            ctx.issue('artifact_gap', 'reset support bytes unavailable')
        elif entry['resolved_at_us'] is None or entry['invalidated_at_us'] is not None:
            ctx.issue(entry['reason'], 'unresolved correctness history', Subject(wire=wire))
    ctx.reset_history = [entries[key] for key in sorted(entries)]
    return tuple(sorted(r['at_us'] for r in ctx.reset_history if r['at_us'] is not None))



def external(ctx: Context) -> None:
    supplied = ctx.bundle.external
    if supplied is None:
        ctx.issue('external_proofs_missing', 'decoded external proofs absent')
        ctx.issue('pilot_prerequisite_gap', 'pilot proof absent')
        return
    names = [p.name for p in supplied.proofs]
    if len(names) != len(set(names)) or set(names) != set(supplied.required) or not names:
        ctx.issue('external_proofs_missing', 'external proof inventory differs')
    for proof in supplied.proofs:
        if (not proof.passed or hashlib.sha256(proof.raw).hexdigest() != proof.sha256 or ctx.proof is None
                or proof.router_revision != ctx.proof['source_shas']['router']
                or proof.enclave_revision != ctx.proof['source_shas']['enclave']):
            ctx.issue('external_proofs_missing', 'external proof hash/revision/verdict differs', Subject(object_id=proof.name))
    if ctx.gate == 'post-flip':
        try:
            require(ctx.ports is not None and supplied.post_input is not None, 'pilot_prerequisite_gap')
            assert ctx.ports is not None and supplied.post_input is not None
            require(invoke_retained(ctx, 'post_flip', supplied.post_input)['status'] == 'PASS', 'pilot_prerequisite_gap')
        except (ValueError, KeyError, TypeError, OSError):
            ctx.issue('pilot_prerequisite_gap', 'retained post-flip status/rollback/handoff failed')
    elif not all((supplied.drain_selected, supplied.signer_checked, supplied.attestation_checked, supplied.exposure_accepted)):
        ctx.issue('pilot_prerequisite_gap', 'selected drain/signer/attestation/exposure incomplete')


def clock(ctx: Context, reset_times: tuple[int, ...]) -> tuple[int | None, int | None, tuple[int, ...]]:
    if ctx.gate not in ('shadow-serving', 'pre-flip'):
        return None, None, ()
    blocked_days = {i.subject.day for i in ctx.issues if i.subject.day is not None}
    gap_times: list[int] = []
    intervals = []
    for manifest in ctx.kind(p1.KINDS[5]):
        if 'router_writers' not in manifest or manifest['day'] not in ctx.bundle.days:
            continue
        for gap in manifest['gaps']:
            start, end = (gap['interval'][k] for k in ('start_us', 'end_us'))
            gap_times.append(start)
            for at in range(start // 86400000000, max(start, end - 1) // 86400000000 + 1):
                blocked_days.add(day_at(at * 86400000000))
        if not manifest['complete']:
            blocked_days.add(manifest['day'])
    for manifest in ctx.kind(p1.KINDS[5]):
        if 'router_writers' not in manifest or manifest['day'] not in ctx.bundle.days or manifest['day'] in blocked_days:
            continue
        day_start = (dt.date.fromisoformat(manifest['day']) - dt.date(1970, 1, 1)).days * 86400000000
        for flag in manifest['disabled_flag_intervals']:
            left = max(day_start, flag['interval']['start_us'])
            right = min(day_start + 86400000000, flag['interval']['end_us'])
            if left < right:
                intervals.append((left, right))
    transport, fleet, pre = (ctx.gates.get(k) for k in ('transport', 'fleet', 'pre'))
    prerequisites = bool(transport and fleet and pre and all(g['verdict'] == 'PASS' for g in (transport, fleet, pre))
                         and ctx.bundle.budget and ctx.bundle.budget.pre_digest == ctx.bundle.budget.post_pre_digest)
    candidates = []
    for sample in ctx.kind(p1.KINDS[0]):
        key = sample['authorization_day'] + '/' + sample['authorization_id']
        ledger = next((entry for entry in ctx.ledgers if entry['boot'] == sample['deployment']['instance']), None)
        if not prerequisites or ledger is None or ctx.ports is None or key not in ctx.verified or not ctx.ports.positive_sample(sample):
            continue
        assert transport is not None and fleet is not None
        threshold = max(transport['completed_at_us'], fleet['completed_at_us'],
                        *(ledger[k]['completed_at_us'] for k in PREREQUISITES))
        at = sample['observed_at_us']
        if at > threshold and day_at(at) in ctx.bundle.days and day_at(at) not in blocked_days and (not reset_times or at > max(reset_times)):
            candidates.append(sample)
    candidates.sort(key=lambda s: s['observed_at_us'])
    persistent = any(i.gap in names('G9') for i in ctx.issues)
    start = candidates[0]['observed_at_us'] if candidates and not persistent else None
    if start is None:
        ctx.issue('positive_seed_gap', 'no qualifying original durable eligible sample')
    end = start
    if start is not None:
        for left, right in sorted(intervals):
            assert end is not None
            if left <= end < right:
                end = min(right, ctx.bundle.completed_at_us)
    elapsed = (end - start) // 1000000 if start is not None and end is not None else None
    if ctx.gate == 'pre-flip' and (elapsed is None or elapsed < 604800):
        ctx.issue('continuous_window_gap', 'less than 604800 continuously covered seconds')
    if ctx.bundle.cohort and ctx.bundle.cohort.regional_seeds and ctx.gate == 'pre-flip':
        traffic_boots = {page['boot'] for page, event in ctx.events()
                         if event['phase'] in ('authorize', 'replay', 'settle', 'refund')}
        regions = {b.region for b in ctx.bundle.inventory.boots
                   if b.role == 'router' and b.boot in traffic_boots} if ctx.bundle.inventory else set()
        seeded = {s['deployment']['region'] for s in candidates if start is not None and start <= s['observed_at_us'] <= (end or start)}
        for region in sorted(regions - seeded):
            ctx.issue('regional_seed_gap', 'region has no real seed', Subject(object_id=region))
    return start, elapsed, tuple(sorted(gap_times))


def _metric(name: str, region: str | None, values: list[int | None]) -> Metric:
    numbers = [v for v in values if v is not None]
    return Metric(name, region, len(values), len(values) - len(numbers), min(numbers) if numbers else None,
                  max(numbers) if numbers else None, sum(numbers) if numbers else None)


def report(ctx: Context, peaks: tuple[Peak, ...], start: int | None, elapsed: int | None,
           reset_times: tuple[int, ...], gap_times: tuple[int, ...]) -> Report:
    writers = []
    for c in ctx.kind(p1.KINDS[3]):
        obs = c['admission_observer']
        e = c['eligible_demand']
        duration = c['flushed_at_us'] - c['started_at_us']
        reads = obs['workspace_reads'] + obs['health_reads']
        failures = sum(obs[k] for k in ('read_failures', 'missed_ticks', 'late_installs'))
        own = [event for page, event in ctx.events() if (page['day'], page['boot']) == (c['day'], c['boot']) and event['phase'] in ('settle', 'refund')]
        writers.append(WriterReport(c['day'], c['boot'], c['A'], c['B'], c['C'], e,
            obs['prediction_yes'], obs['prediction_no'], obs['prediction_unknown'],
            'unknown' if e is None else 'not_exercised' if e == 0 else 'exercised',
            obs['prediction_unknown'] / e if e else None, failures / reads if reads else None,
            obs['degraded_seconds'] * 1000000 / duration if duration else None,
            sum(v['retry_original'] is None for v in own), sum(v['retry_original'] is not None for v in own),
            c['checkpoint_failures'], c['checkpoint_recoveries'], c['loss_count'] + c['irrecoverable_losses']))
    if ctx.bundle.inventory is not None:
        present = {(w.day, w.boot) for w in writers}
        for boot in ctx.bundle.inventory.boots:
            for day in ctx.bundle.days:
                if (boot.role == 'router' and day_at(boot.start_us) <= day
                        and (boot.end_us is None or day <= day_at(boot.end_us - 1))
                        and (day, boot.boot) not in present):
                    writers.append(WriterReport(day, boot.boot, None, None, None, None, None, None,
                        None, 'unknown', None, None, None, None, None, None, None, None))
    samples = ctx.kind(p1.KINDS[0])
    regions = tuple(sorted({b.region for b in ctx.bundle.inventory.boots if b.role == 'router'})) if ctx.bundle.inventory else None
    timing = tuple(_metric(field, region, [s['timing'][field] for s in samples if s['deployment']['region'] == region])
        for region in (regions or ()) for field in ('authorize_shadow_us', 'handoff_prepare_us', 'router_settle_us', 'booking_confirm_us', 'comparator_us', 'evidence_write_us'))
    deltas = tuple(_metric(field, None, [s[field] for s in samples]) for field in ('python_minus_go', 'booked_minus_frozen', 'rebuilt_minus_frozen', 'booked_minus_rebuilt'))
    valid = bool(writers) and not any(i.gap == 'evidence_schema_gap' for i in ctx.issues)
    eligible = sum(w.eligible or 0 for w in writers) if valid and all(w.eligible is not None for w in writers) else None
    compared = sum(w.a or 0 for w in writers) if valid and all(w.a is not None for w in writers) else None
    known = sum((w.yes or 0) + (w.no or 0) for w in writers) if valid and all(w.yes is not None and w.no is not None for w in writers) else None
    return Report(compared, eligible, known, eligible, tuple(writers), peaks or None, regions, timing, deltas,
                  start, reset_times, gap_times, start, elapsed,
                  sum(o['status'] != 'terminal_reconciled' for o in ctx.obligations) if valid else None)


def evaluate(bundle: Bundle | Any, ports: Ports | None = None, *, gate: str = 'pre-flip') -> Result:
    require(gate in STAGES, 'artifact_gap')
    if not isinstance(bundle, Bundle):
        bundle = Bundle(undefined_preimages=('bundle',))
    ctx = decode_bundle(bundle, ports, gate)
    needed = ('inventory', 'cohort', 'budget') if gate == 'fleet-budgets-pre' else ('inventory', 'cohort', 'budget', 'external', 'reset_support')
    for name in needed:
        if getattr(bundle, name) is None:
            ctx.issue('artifact_gap', 'undefined preimage: ' + name, Subject(object_id=name))
    # Collect independent predicates even after prior failures; no early verdict.
    for collect in (coverage_issues, verify_samples, deployment, receipt_accounting, authorization, observer_health, durability):
        try:
            collect(ctx)
        except (ValueError, KeyError, TypeError, OverflowError) as exc:
            ctx.issue('evidence_schema_gap', collect.__name__ + ': ' + type(exc).__name__)
    check_calls(ctx)
    peaks = load_budgets(ctx)
    reset_times = resets(ctx)
    external(ctx)
    start, elapsed, gap_times = clock(ctx, reset_times)
    selected = STAGES[gate]
    window = None
    populations = None
    try:
        window = a2report.window_for(ctx)
        populations = a2report.population(ctx, window, selected)
    except ValueError:
        ctx.issue('artifact_gap', 'report observation window unavailable')
    normalize(ctx)
    if any(i.gap == 'evidence_schema_gap' for i in ctx.issues):
        ctx.issue('artifact_gap', 'incomplete evaluation after schema damage')
    failures = collect_failures(ctx, selected)
    metrics = report(ctx, peaks, start, elapsed, reset_times, gap_times)
    gate_report = None
    streams: dict[str, list[dict[str, Any]]] = {}
    leaves: dict[str, bytes] = {}
    if window is not None and populations is not None and ctx.proof is not None:
        gate_report, streams, leaves = a2report.build(ctx, failures, metrics, window, populations, records(ctx))
        unavailable: list[str] = []
        for rule, paths in PASS_AGGREGATES[gate].items():
            if gate_report['rules'][rule] in ('not_required', 'unevaluable'):
                continue
            for path in paths:
                value = gate_report
                for part in path.split('.'):
                    value = value[part]
                if value is None:
                    unavailable.append(rule)
                    gap = {'G6': 'publisher_trace_gap', 'G7': 'close_handoff_budget_gap' if path.startswith('timing.') else 'fleet_headroom_gap',
                           'G8': 'handoff_coverage_gap' if path.startswith(('handoff.', 'timing.handoff', 'timing.drain')) else 'writer_closure_gap'}.get(rule, names(rule)[0])
                    ctx.issue(gap, 'required aggregate unavailable: ' + path, unavailable=True)
                    break
        if unavailable:
            ctx.issue('artifact_gap', 'required report aggregate unavailable')
            normalize(ctx)
            failures = collect_failures(ctx, selected)
            gate_report, streams, leaves = a2report.build(ctx, failures, metrics, window, populations, records(ctx))
            for rule in unavailable:
                gate_report['rules'][rule] = 'unevaluable'

    return Result(gate, bundle.completed_at_us, file_digest(bundle.proof) if bundle.proof else None,
                  ctx.proof['ledger_root'] if ctx.proof else None,
                  tuple(sorted({f.gap for f in failures})), failures, metrics, gate_report, streams, leaves)


def fleet_budgets_pre(bundle: Bundle, ports: Ports | None = None) -> Result:
    return evaluate(bundle, ports, gate='fleet-budgets-pre')


def fleet_budgets(bundle: Bundle, ports: Ports | None = None) -> Result:
    return evaluate(bundle, ports, gate='fleet-budgets')


def shadow_serving(bundle: Bundle, ports: Ports | None = None) -> Result:
    return evaluate(bundle, ports, gate='shadow-serving')


def pre_flip(bundle: Bundle, ports: Ports | None = None) -> Result:
    return evaluate(bundle, ports, gate='pre-flip')


def post_flip(bundle: Bundle, ports: Ports | None = None) -> Result:
    return evaluate(bundle, ports, gate='post-flip')
