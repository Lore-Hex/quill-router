"""Simultaneous half-open fleet sweep and measured load/health predicates."""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as h
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_bundle import Context, subject
from trusted_router.async_settle_shadow_v2_gate_inputs import Boot, Rate
from trusted_router.async_settle_shadow_v2_gate_loading import error_detail
from trusted_router.async_settle_shadow_v2_gate_result import Peak, Subject
from trusted_router.async_settle_shadow_v2_gate_verifiers import invoke_retained
from trusted_router.async_settle_shadow_v2_schema import day_at, require, sha256

COMPONENT_BOUNDS = dict(publisher_period=2., publisher_jitter=.25, publication=.5,
    poll_period=1., poll_jitter=.25, install=.25, workspace_period=4.,
    workspace_jitter=.25, workspace_install=.5)


def sweep(boots: tuple[Boot, ...], rates: tuple[Rate, ...], workspace_count: int,
          completed_at_us: int) -> tuple[Peak, ...]:
    """Evaluate every endpoint/UTC boundary/rate boundary, including burst buckets."""
    require(type(workspace_count) is int and 1 <= workspace_count <= 32, 'simultaneous_capacity_gap')
    require(len({b.boot for b in boots}) == len(boots), 'simultaneous_capacity_gap')
    routers = [b for b in boots if b.role == 'router']
    edges = {completed_at_us}
    for boot in routers:
        end = boot.end_us if boot.end_us is not None else completed_at_us
        require(0 <= boot.start_us < end, 'simultaneous_capacity_gap')
        edges.update((boot.start_us, end))
        edges.update(range((boot.start_us // 86400000000 + 1) * 86400000000, end, 86400000000))
        segments = sorted((r for r in rates if r.boot == boot.boot), key=lambda r: r.start_us)
        for rate in segments:
            require(boot.start_us <= rate.start_us < rate.end_us <= end, 'simultaneous_capacity_gap')
            require(all(type(v) is int and v >= 0 for v in (rate.workspace_reads, rate.health_reads,
                    rate.publisher_reads, rate.publisher_writes, rate.burst_reads, rate.burst_pending)), 'simultaneous_capacity_gap')
            edges.update((rate.start_us, rate.end_us, min(rate.end_us, rate.start_us + 1000000)))
        require(all(a.end_us <= b.start_us for a, b in zip(segments, segments[1:], strict=False)), 'simultaneous_capacity_gap')
    require(all(any(b.boot == r.boot for b in routers) for r in rates), 'simultaneous_capacity_gap')
    peaks: dict[str | None, list[Fraction]] = {None: [Fraction(0)] * 8}
    for b in routers:
        peaks.setdefault(b.region, [Fraction(0)] * 8)
    for at in sorted(edges)[:-1]:
        totals: dict[str | None, list[Fraction]] = {k: [Fraction(0)] * 8 for k in peaks}
        for boot in routers:
            if not boot.start_us <= at < (boot.end_us if boot.end_us is not None else completed_at_us):
                continue
            nominal = Fraction(workspace_count, 4)
            trust, health, pending, pr, pw = nominal, Fraction(1), 1001 * nominal, Fraction(0), Fraction(0)
            burst_reads = Fraction(0)
            for rate in rates:
                if rate.boot == boot.boot and rate.start_us <= at < rate.end_us:
                    duration = Fraction(rate.end_us - rate.start_us, 1000000)
                    trust = max(trust, rate.workspace_reads / duration)
                    health = max(health, rate.health_reads / duration)
                    pending = max(pending, 1001 * rate.workspace_reads / duration)
                    pr, pw = rate.publisher_reads / duration, rate.publisher_writes / duration
                    if at < rate.start_us + 1000000:
                        burst_reads = Fraction(rate.burst_reads)
                        pending = max(pending, Fraction(rate.burst_pending))
            values = (Fraction(1), max(trust + health + pr, burst_reads), pending, trust, health, pr, pw, Fraction(boot.contender))
            for region in (None, boot.region):
                for i, value in enumerate(values):
                    totals[region][i] += value
        for region, segment_values in totals.items():
            peaks[region] = [max(old, value) for old, value in zip(peaks[region], segment_values, strict=True)]
    return tuple(Peak(region, int(v[0]), v[1], v[2], v[3], v[4], v[5], v[6], int(v[7])) for region, v in sorted(peaks.items(), key=lambda item: item[0] or ''))


def observer_health(ctx: Context) -> None:
    n = ctx.bundle.cohort.workspace_count if ctx.bundle.cohort else None
    for counter in ctx.kind(p1.KINDS[3]):
        sub = subject(counter)
        obs = counter['admission_observer']
        duration_us = counter['flushed_at_us'] - counter['started_at_us']
        duration = duration_us / 1000000
        if (n is None or duration <= 0 or not counter['closed']
                or not max(1, n * (math.floor(duration / 4) - 1) - obs['missed_ticks']) <= obs['workspace_reads'] <= n * (math.ceil(duration / 4) + 1)
                or not max(1, math.floor(duration) - 1 - obs['missed_ticks']) <= obs['health_reads'] <= math.ceil(duration) + 1):
            ctx.issue('observer_cadence_gap', 'observer cadence outside bounds', sub)
        reads = obs['health_reads'] + obs['workspace_reads']
        failures = sum(obs[k] for k in ('read_failures', 'missed_ticks', 'late_installs'))
        if failures * 100 > max(300, reads) or obs['max_consecutive_failures'] > 2 or obs['degraded_seconds'] * 100000000 > duration_us:
            ctx.issue('observer_budget_gap', 'observer failure/streak/degraded budget', sub)
        entries = [e for p in ctx.attached('publisher_trace') for e in p['entries'] if all(p[k] == counter[k] for k in ('day', 'boot'))]
        entries.sort(key=lambda e: e['slot'])
        boot = next((b for b in ctx.bundle.inventory.boots if b.boot == counter['boot']), None) if ctx.bundle.inventory else None
        first = (counter['started_at_us'] + 1999999) // 2000000 if boot and boot.contender else None
        last = (counter['flushed_at_us'] - 1) // 2000000 if boot and boot.contender else None
        if first is not None and last is not None and last < first:
            first = last = None
        try:
            h.validate_publisher_traces(counter['publisher'], entries, day=counter['day'], boot=counter['boot'], first_slot=first, last_slot=last)
        except ValueError as exc:
            ctx.issue('publisher_accounting_gap' if error_detail(exc) in ('publisher_accounting_gap', 'a1-publisher-conservation') else 'publisher_trace_gap', error_detail(exc), sub)
        pub = counter['publisher']
        failed = pub['failures'] + pub['missed_slots']
        streak = peak = 0
        for entry in entries:
            streak = streak + 1 if entry['outcome'] in ('failed', 'missed') else 0
            peak = max(peak, streak)
        if 100 * failed > max(300, pub['scheduled_slots']) or peak > 2 or failed * 200000000 > duration_us:
            ctx.issue('observer_budget_gap', 'publisher availability budget', sub)
    for stream, sequence_name, previous_name in (
        ('publisher_trace', 'first_slot', 'previous_sha256'),
        ('health_basis', 'sequence', 'previous_sha256'),
    ):
        groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for page in ctx.attached(stream):
            groups.setdefault((page['day'], page['boot']), []).append(page)
        for (day, boot_name), pages in groups.items():
            previous_hash = None
            for page in sorted(pages, key=lambda p: p[sequence_name]):
                if page[previous_name] != previous_hash:
                    ctx.issue('publisher_trace_gap' if stream == 'publisher_trace' else 'shadow_health_gap',
                              'attachment chain differs', Subject(day, boot_name))
                previous_hash = sha256(page)
    bases = {sha256(b): b for p in ctx.attached('health_basis') for b in p['bases']}
    traces = [e for p in ctx.attached('publisher_trace') for e in p['entries']]
    for page, event in ctx.events():
        prediction = event.get('prediction')
        if prediction is None:
            continue
        sub = subject(page, event['seq'])
        basis = bases.get(prediction['basis_sha256'])
        if basis is None:
            ctx.issue('shadow_health_gap', 'missing durable prediction basis', sub)
            continue
        if basis['reader_boot'] != page['boot'] or prediction['health_source'] != 'shadow_observation':
            ctx.issue('health_source_gap', 'wrong reader or source', sub)
        if basis['body'] is None:
            if prediction['prediction'] != 'unknown':
                ctx.issue('shadow_health_gap', 'known prediction without usable source', sub)
            continue
        body = basis['body']
        metadata = {k: basis[k] for k in h.HEALTH_READ_METADATA.fields}
        usable = h.health_usable(body, metadata, basis['read_elapsed_ns'], basis['since_reply_ns'])
        if not usable and prediction['prediction'] != 'unknown':
            ctx.issue('shadow_health_gap', 'known prediction from unusable observation', sub)
        if basis['read_elapsed_ns'] > 250000000 or basis['read_elapsed_ns'] + basis['since_reply_ns'] > 1500000000:
            ctx.issue('health_elapsed_gap', 'elapsed read/cache bound', sub)
        if not body['observed_at_ns'] <= basis['committed_at_ns'] <= basis['read_at_ns'] or (basis['read_at_ns'] - body['observed_at_ns'] + basis['read_elapsed_ns'] + basis['since_reply_ns']) >= 5000000000:
            ctx.issue('health_timestamp_gap', 'snapshot age/order bound', sub)
        trace = next((t for t in traces if t['boot'] == body['publisher_boot_id'] and t['slot'] == body['slot']), None)
        if trace is None:
            ctx.issue('publisher_trace_gap', 'basis has no publisher trace',
                      Subject(day_at(body['slot'] * 2000000), body['publisher_boot_id']))
        else:
            try:
                h.validate_basis_trace(basis, trace)
                claim = {'v': 2, 'policy_version': 'shadow-v2', 'contract_revision': 'a1', 'authority': 'local',
                         'slot': trace['slot'], 'owner_boot_id': trace['boot'], 'claim_at_ns': trace['claim_at_ns']}
                h.validate_publication(claim, body)
                require(prediction['health_slot'] == body['slot'], 'health_slot_gap')
            except ValueError as exc:
                ctx.issue('health_slot_gap' if error_detail(exc) == 'health_slot_gap' else 'publisher_trace_gap', error_detail(exc),
                          sub if error_detail(exc) == 'health_slot_gap' else Subject(trace['day'], trace['boot']))
    # Every scheduled fleet slot needs an actual winner, independent of reader success.
    by_slot: dict[int, list[dict[str, Any]]] = {}
    for trace in traces:
        by_slot.setdefault(trace['slot'], []).append(trace)
    for slot, entries in by_slot.items():
        if not any(e['body_sha256'] is not None for e in entries):
            ctx.issue('shadow_health_gap', 'fleet winning slot absent', Subject(day=entries[0]['day'], object_id=str(slot)))


def load_budgets(ctx: Context) -> tuple[Peak, ...]:
    budget, cohort, inventory = ctx.bundle.budget, ctx.bundle.cohort, ctx.bundle.inventory
    if budget is None or cohort is None or inventory is None:
        ctx.issue('pre_enable_budget', 'decoded budget/cohort/inventory absent')
        return ()
    try:
        require(ctx.ports is not None, 'pre_enable_budget')
        assert ctx.ports is not None
        legacy = invoke_retained(ctx, 'fleet_budgets_pre', budget.pre_input)
        require(legacy.get('status') == 'PASS', 'pre_enable_budget')
        require(legacy['workspace_count'] == cohort.workspace_count == budget.post_workspace_count
                and legacy['maximum_router_instances_by_region'] == dict(budget.regional_caps) == dict(budget.post_regional_caps)
                and budget.pre_digest == budget.post_pre_digest
                and budget.pre_completed_us <= budget.post_completed_us
                and budget.fleet_reads == legacy['approved_fleet_reads_per_second']
                and dict(budget.regional_reads) == {r: v['reads_per_second'] for r, v in legacy['regional_budgets'].items()}
                and dict(budget.regional_pending) == {r: v['pending_rows_per_second'] for r, v in legacy['regional_budgets'].items()}, 'pre_enable_budget')
    except (ValueError, KeyError, TypeError, OSError):
        ctx.issue('pre_enable_budget', 'retained budget/approval check failed')
    peaks: tuple[Peak, ...] = ()
    try:
        peaks = sweep(inventory.boots, budget.rates, cohort.workspace_count, ctx.bundle.completed_at_us)
        require(sum(budget.regional_caps.values()) == budget.fleet_cap, 'simultaneous_capacity_gap')
        for peak in peaks:
            cap = budget.fleet_cap if peak.region is None else budget.regional_caps.get(peak.region, 0)
            if peak.simultaneous > cap:
                ctx.issue('simultaneous_capacity_gap', 'simultaneous cap exceeded', Subject(object_id=peak.region))
    except ValueError as exc:
        ctx.issue('simultaneous_capacity_gap', error_detail(exc))
    maxima = budget.component_seconds
    if (set(maxima) != set(COMPONENT_BOUNDS) or any(not math.isfinite(v) or not 0 <= v <= COMPONENT_BOUNDS[k] for k, v in maxima.items() if k in COMPONENT_BOUNDS)
            or sum(maxima.get(k, math.inf) for k in ('publisher_period', 'publisher_jitter', 'publication', 'poll_period', 'poll_jitter', 'install')) > 4.25
            or sum(maxima.get(k, math.inf) for k in ('workspace_period', 'workspace_jitter', 'workspace_install')) > 4.75):
        ctx.issue('freshness_budget_gap', 'component or composed freshness budget')
    primary = [b for b in inventory.boots if b.contender]
    if any(b.region != cohort.primary_region for b in primary) or not primary:
        ctx.issue('publisher_budget_gap', 'no primary-region publisher')
    router_intervals = [(b.start_us, b.end_us or ctx.bundle.completed_at_us) for b in inventory.boots if b.role == 'router']
    for left, right in router_intervals:
        covered = left
        for boot in sorted(primary, key=lambda b: b.start_us):
            if boot.start_us <= covered:
                covered = max(covered, boot.end_us or ctx.bundle.completed_at_us)
        if covered < right:
            ctx.issue('publisher_budget_gap', 'primary publisher lifetime coverage hole')
    if any(p.publisher_simultaneous > budget.publisher_cap for p in peaks) or budget.publisher_cap < 1 or budget.publisher_reads < budget.publisher_cap / 2 + 1.5 or budget.publisher_writes < 1 or any(p.publisher_reads > budget.publisher_reads or p.publisher_writes > budget.publisher_writes for p in peaks):
        ctx.issue('publisher_budget_gap', 'publisher cap/rate/cycle budget')
    for page in ctx.attached('publisher_trace'):
        if any(e['cycle_us'] is not None and e['cycle_us'] > 500000 for e in page['entries']):
            ctx.issue('publisher_budget_gap', 'publisher cycle deadline', subject(page))
    publisher_bounds = (
        (budget.publisher_unresolved_rows_per_second, 5000.5),
        (budget.publisher_control_rows_per_second, budget.publisher_cap / 2 + 1),
        (budget.publisher_transactions_per_second, budget.publisher_cap / 2 + .5),
        (budget.metadata_bytes_per_second, 16 * (budget.fleet_cap + budget.surge) + 4 * budget.publisher_cap),
    )
    if any(actual is None or not math.isfinite(actual) or actual < required for actual, required in publisher_bounds):
        ctx.issue('publisher_budget_gap', 'publisher rows/transactions/metadata allowance absent or insufficient')
    rpc_peak = max((sum(e['rpc_count'] for e in p['entries']) / (2 * len(p['entries']))
                    for p in ctx.attached('publisher_trace')), default=0.)
    if budget.publisher_rpc_per_second is None or not math.isfinite(budget.publisher_rpc_per_second) or rpc_peak > budget.publisher_rpc_per_second:
        ctx.issue('publisher_budget_gap', 'measured native publisher RPC allowance')
    nominal = cohort.workspace_count / 4 + 1
    if (not math.isfinite(budget.cpu_peak + budget.cpu_increment) or budget.cpu_increment <= 0
            or budget.cpu_peak + budget.cpu_increment >= 45
            or (budget.fleet_cap + budget.surge) * nominal + budget.publisher_reads > budget.fleet_reads):
        ctx.issue('fleet_headroom_gap', 'configured cap/surge or strict CPU headroom')
    for region, cap in budget.regional_caps.items():
        projected_reads = (cap + budget.surge) * nominal
        if region == cohort.primary_region:
            projected_reads += budget.publisher_reads
        if projected_reads > budget.regional_reads.get(region, -1) or (cap + budget.surge) * cohort.workspace_count / 4 * 1001 > budget.regional_pending.get(region, -1):
            ctx.issue('fleet_headroom_gap', 'regional configured-cap projection', Subject(object_id=region))
    for p in peaks:
        reads = budget.fleet_reads if p.region is None else budget.regional_reads.get(p.region, -1)
        pending = math.inf if p.region is None else budget.regional_pending.get(p.region, -1)
        if p.reads > reads or p.pending > pending:
            ctx.issue('fleet_headroom_gap', 'measured simultaneous load exceeds approval', Subject(object_id=p.region))
    actual_ops = len(ctx.bundle.rows) + len(ctx.bundle.attachments) + len(ctx.bundle.ledgers) + 1
    actual_bytes = sum(len(r.raw) for r in (*ctx.bundle.rows, *ctx.bundle.attachments)) + sum(map(len, ctx.bundle.ledgers)) + len(ctx.bundle.proof or b'')
    prior_originals = [raw for name in ('transport', 'fleet', 'pre', 'serving')
                       if (raw := getattr(ctx.bundle, name)) is not None]
    actual_ops += len(prior_originals)
    actual_bytes += sum(map(len, prior_originals))
    actual_ops += len(ctx.bundle.preimages)
    actual_bytes += sum(len(p.raw) for p in ctx.bundle.preimages)
    actual_ops = (actual_ops + budget.export_root_writes) * budget.export_passes
    actual_bytes = (actual_bytes + budget.export_write_bytes) * budget.export_passes
    accounting = ctx.bundle.a2_streams.get('export_accounting', [])
    if accounting:
        # Every declared pass contributes its actual calls, including reruns.
        native_records = sum(max(p['source_records'] for p in accounting if p['day'] == day)
                             for day in {p['day'] for p in accounting})
        actual_ops += sum(p['native_rpcs'] for p in accounting) - native_records * budget.export_passes
        actual_bytes = max(actual_bytes, sum(p['body_bytes'] for p in accounting) + budget.export_write_bytes)
    invalid_export = (budget.export_passes < 1 or budget.export_root_writes < 0
        or not 0 <= budget.export_write_bytes <= 262144 * budget.export_root_writes
        or not 0 <= budget.cleanup_seconds <= 30
        or not 0 <= budget.cleanup_spanner_keys <= 1000 * budget.cleanup_seconds
        or math.ceil(budget.cleanup_spanner_keys / 200) > 5 * budget.cleanup_seconds
        or not 0 <= budget.cleanup_sink_keys <= 1000 * budget.cleanup_seconds)
    if invalid_export or actual_ops > budget.export_operations or actual_bytes > budget.export_bytes:
        ctx.issue('evidence_export_budget_gap', 'bounded export exceeds approved finite total')
    closes = len(ctx.kind(p1.KINDS[4]))
    if budget.close_transactions < 2 * closes or budget.close_rows < 7 * closes or budget.close_elapsed_us > 2000000 or budget.handoff_elapsed_us > 2000000:
        ctx.issue('close_handoff_budget_gap', 'separate final/close-result transaction budget')
    shutdown = ctx.bundle.shutdown
    if shutdown is not None:
        entries = {(s['day'], s['boot']): s for s in shutdown.entries}
        originals = {(s['day'], s['boot']): s for s in ctx.kind(a1.KINDS[-1])}
        for measured in shutdown.measurements:
            close, result, final = (measured[k] for k in ('close_us', 'result_us', 'final_pass_us'))
            entry = entries[measured['day'], measured['boot']]
            grace = entry['grace_us'] if entry['termination_applies'] else measured['grace_us']
            original = originals.get((measured['day'], measured['boot']))
            remaining_ok = True
            if entry['close_result'] is not None:
                remaining_ok = original is not None and (original['shutdown_remaining_us'] is not None
                    or not (entry['termination_applies'] or entry['reason'] == 'shutdown'))
                if original is not None and original['shutdown_remaining_us'] is not None:
                    # Captured at original-attempt start: preparation and wait
                    # are already spent. The following result is still due.
                    remaining_ok = max(close, original['elapsed_us']) + result <= original['shutdown_remaining_us']
            if not (remaining_ok and 0 <= close <= 2000000 and 0 <= result <= 2000000
                    and close + result <= final <= 4000000
                    and measured['preparation_us'] + measured['export_wait_us'] + final <= min(grace, 9000000)):
                ctx.issue('close_handoff_budget_gap', 'per-measurement final pass and remaining grace',
                    Subject(day=measured['day'], boot=measured['boot']))
    compare_peak_limits(ctx, peaks)
    return peaks


def measured_peaks(ctx: Context, region: str | None, *, publisher: bool = False) -> dict[str, Any]:
    """Sum simultaneous load evidence, then maximize; never sum historical peaks.

    LOAD_SEGMENT rates are conservative bounds on their half-open intervals.
    Null in any active contributor invalidates that component for the scope.
    Configured projections and measured CPU come from the approved Budget.
    """
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    budget, cohort = ctx.bundle.budget, ctx.bundle.cohort
    independent = ('simultaneous_routers', 'simultaneous_publishers', 'reads_per_second',
                   'pending_rows_per_second', 'trust_rows_per_second', 'health_rows_per_second')
    result: dict[str, Any] = {k: None for k in a2.PEAKS.fields if k not in independent}
    if budget is None or cohort is None:
        return result
    projected = ctx.gate == 'fleet-budgets-pre'
    values = budget.projected_load_segments if projected else budget.load_segments
    try:
        require(values is not None)
        assert values is not None
        for segment in values:
            a2.LOAD_SEGMENT(segment)
        expected = {(s['boot'], s['start_us'], s['end_us']) for s in values if s['role'] == 'router' and s['start_us'] < s['end_us']} if projected else {(r.boot, r.start_us, r.end_us) for r in budget.rates}
        require(expected == {(s['boot'], s['start_us'], s['end_us']) for s in values if s['role'] == 'router' and s['start_us'] < s['end_us']})
        require(len(values) == len({(s['boot'], s['start_us'], s['end_us']) for s in values}))
        boots = {b.boot: b for b in ctx.bundle.inventory.boots} if ctx.bundle.inventory else {}
        require(projected or all(s['boot'] in boots and s['region'] == boots[s['boot']].region
            and s['contender'] == boots[s['boot']].contender and s['role'] == boots[s['boot']].role
            and s['workspace_count'] == cohort.workspace_count and s['day'] == day_at(s['start_us']) for s in values))
    except (ValueError, KeyError, TypeError):
        values = None
    scoped = tuple(s for s in values or () if s['role'] == 'router' and (region is None or s['region'] == region) and (not publisher or s['contender']))
    fields = tuple(a2.PEAKS.fields)
    measured = fields[4:7] + fields[11:16]
    maxima: dict[str, Fraction | None] = dict.fromkeys(measured, Fraction(0) if values is not None else None)
    edges = sorted({s[k] for s in scoped for k in ('start_us', 'end_us')})
    for at in edges[:-1]:
        active = [s for s in scoped if s['start_us'] <= at < s['end_us']]
        for name in measured:
            parts = [s['peaks'][name] for s in active]
            old = maxima[name]
            if old is None or any(p is None for p in parts):
                maxima[name] = None
            else:
                maxima[name] = max(old, sum((Fraction(p['numerator'], p['denominator']) if isinstance(p, dict) else Fraction(p) for p in parts), Fraction(0)))
    cap = budget.fleet_cap if region is None else budget.regional_caps.get(region)
    publishers = budget.publisher_cap if region is None or region == cohort.primary_region else 0
    result.update(configured_routers=None if cap is None else cap + budget.surge, configured_publishers=publishers)
    for name, number in maxima.items():
        result[name] = None if number is None else int(number) if name.endswith('_1s') else dict(numerator=number.numerator, denominator=number.denominator)
    configured = 0 if cap is None else cap + budget.surge
    projections = {
        'configured_reads_per_second': lambda: Fraction(configured) * (Fraction(cohort.workspace_count, 4) + 1) + (Fraction(budget.publisher_reads) if publishers else 0),
        'configured_pending_rows_per_second': lambda: Fraction(configured * 1001 * cohort.workspace_count, 4),
        'cpu_percent': lambda: Fraction(budget.cpu_peak),
    }
    for name, project in projections.items():
        try:
            number = project()
            result[name] = dict(numerator=number.numerator, denominator=number.denominator)
        except (ValueError, OverflowError, TypeError):
            result[name] = None
    for name, check in a2.PEAKS.fields.items():
        if name not in result:
            continue
        try:
            check(result[name])
        except ValueError:
            result[name] = None
    if projected:
        result.update(projected_independent_peaks(ctx, region))
    if cap is None:
        result['configured_reads_per_second'] = result['configured_pending_rows_per_second'] = None
    return result



def compare_peak_limits(ctx: Context, actual: tuple[Peak, ...]) -> None:
    """Consume every contract PEAKS component in exact arithmetic at each K."""
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router.async_settle_shadow_v2_gate_report import peaks as report_peaks
    budget = ctx.bundle.budget
    if budget is None:
        return
    stage = 'pre' if ctx.gate == 'fleet-budgets-pre' else 'post'
    if stage == 'post':
        projected = Context(ctx.bundle, ctx.ports, 'fleet-budgets-pre', issues=ctx.issues)
        compare_peak_limits(projected, actual)
    limits = budget.peak_limits.get(stage)
    scopes = [None, *sorted(budget.regional_caps)]
    for region in scopes:
        sub = Subject(wire=a2.K(stage, region))
        approved = (limits.get('fleet') if region is None else next((r['approved']
            for r in limits.get('regions', []) if r['region'] == region), None)) if limits else None
        values = report_peaks(next((p for p in actual if p.region == region), None), ctx)
        for name in a2.PEAKS.fields:
            value, limit = values.get(name), approved.get(name) if approved else None
            if value is None or limit is None:
                ctx.issue('fleet_headroom_gap', stage + ' unavailable peak or limit: ' + name,
                          sub, unavailable=True)
                ctx.issue('artifact_gap', 'required approved peak comparison unavailable',
                          Subject(wire=a2.O('a2_file', 'budgets/limits.json')))
            elif peak_number(value) > peak_number(limit):
                gap = ('simultaneous_capacity_gap' if name in ('simultaneous_routers',
                    'simultaneous_publishers', 'configured_routers', 'configured_publishers',
                    'startup_operations_1s') else 'fleet_headroom_gap')
                ctx.issue(gap, stage + ' approved peak exceeded: ' + name, sub)


def peak_number(value: Any) -> Fraction:
    return Fraction(value['numerator'], value['denominator']) if isinstance(value, dict) else Fraction(value)



def projected_independent_peaks(ctx: Context, region: str | None) -> dict[str, Any]:
    """Pre-enable report and comparison share the same projected population."""
    segments = ctx.bundle.budget.projected_load_segments if ctx.bundle.budget else None
    names = ('simultaneous_routers', 'simultaneous_publishers', 'reads_per_second',
             'pending_rows_per_second', 'trust_rows_per_second', 'health_rows_per_second')
    selected = [s for s in segments or () if s['role'] == 'router'
                and (region is None or s['region'] == region)]
    edges = sorted({s[k] for s in selected for k in ('start_us', 'end_us')})
    values = {}
    for name in names:
        peak: Fraction | None = Fraction(0) if segments is not None else None
        for at in edges[:-1]:
            parts = [s['peaks'][name] for s in selected if s['start_us'] <= at < s['end_us']]
            if peak is None or any(v is None for v in parts):
                peak = None
            else:
                peak = max(peak, sum((peak_number(v) for v in parts), Fraction(0)))
        values[name] = None if peak is None else int(peak) if name.startswith('simultaneous_') else dict(
            numerator=peak.numerator, denominator=peak.denominator)
    return values
