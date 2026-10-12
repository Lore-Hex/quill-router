"""Simultaneous half-open fleet sweep and measured load/health predicates."""
from __future__ import annotations

import math
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_health as h
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_bundle import Context, subject
from trusted_router.async_settle_shadow_v2_gate_inputs import Boot, Rate
from trusted_router.async_settle_shadow_v2_gate_result import Peak, Subject
from trusted_router.async_settle_shadow_v2_schema import require, sha256

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
    peaks: dict[str | None, list[float]] = {None: [0.] * 8}
    for b in routers:
        peaks.setdefault(b.region, [0.] * 8)
    for at in sorted(edges)[:-1]:
        totals: dict[str | None, list[float]] = {k: [0.] * 8 for k in peaks}
        for boot in routers:
            if not boot.start_us <= at < (boot.end_us if boot.end_us is not None else completed_at_us):
                continue
            nominal = workspace_count / 4
            trust, health, pending, pr, pw = nominal, 1., 1001 * nominal, 0., 0.
            burst_reads = 0.
            for rate in rates:
                if rate.boot == boot.boot and rate.start_us <= at < rate.end_us:
                    duration = (rate.end_us - rate.start_us) / 1000000
                    trust = max(trust, rate.workspace_reads / duration)
                    health = max(health, rate.health_reads / duration)
                    pending = max(pending, 1001 * rate.workspace_reads / duration)
                    pr, pw = rate.publisher_reads / duration, rate.publisher_writes / duration
                    if at < rate.start_us + 1000000:
                        burst_reads = rate.burst_reads
                        pending = max(pending, rate.burst_pending)
            values = (1., max(trust + health + pr, burst_reads), pending, trust, health, pr, pw, float(boot.contender))
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
            ctx.issue('publisher_accounting_gap' if str(exc) in ('publisher_accounting_gap', 'a1-publisher-conservation') else 'publisher_trace_gap', str(exc), sub)
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
            ctx.issue('publisher_trace_gap', 'basis has no publisher trace', sub)
        else:
            try:
                h.validate_basis_trace(basis, trace)
                claim = {'v': 2, 'policy_version': 'shadow-v2', 'contract_revision': 'a1', 'authority': 'local',
                         'slot': trace['slot'], 'owner_boot_id': trace['boot'], 'claim_at_ns': trace['claim_at_ns']}
                h.validate_publication(claim, body)
                require(prediction['health_slot'] == body['slot'], 'health_slot_gap')
            except ValueError as exc:
                ctx.issue('health_slot_gap' if str(exc) == 'health_slot_gap' else 'publisher_trace_gap', str(exc), sub)
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
        legacy = ctx.ports.fleet_budgets_pre(budget.pre_input)
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
            require(peak.simultaneous <= cap, 'simultaneous_capacity_gap')
    except ValueError as exc:
        ctx.issue('simultaneous_capacity_gap', str(exc))
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
    if any(p.publisher_simultaneous > budget.publisher_cap for p in peaks) or budget.publisher_cap < 1 or budget.publisher_reads < budget.publisher_cap / 2 + 1.5 or budget.publisher_writes < 1 or any(p.publisher_reads > budget.publisher_reads or p.publisher_writes > budget.publisher_writes for p in peaks) or any(e['cycle_us'] is not None and e['cycle_us'] > 500000 for p in ctx.attached('publisher_trace') for e in p['entries']):
        ctx.issue('publisher_budget_gap', 'publisher cap/rate/cycle budget')
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
    actual_ops += len(ctx.bundle.preimages)
    actual_bytes += sum(len(p.raw) for p in ctx.bundle.preimages)
    actual_ops = (actual_ops + budget.export_root_writes) * budget.export_passes
    actual_bytes = (actual_bytes + budget.export_write_bytes) * budget.export_passes
    invalid_export = (budget.export_passes < 1 or budget.export_root_writes < 0
        or not 0 <= budget.export_write_bytes <= 262144 * budget.export_root_writes
        or not 0 <= budget.cleanup_seconds <= 30
        or not 0 <= budget.cleanup_spanner_keys <= 1000 * budget.cleanup_seconds
        or math.ceil(budget.cleanup_spanner_keys / 200) > 5 * budget.cleanup_seconds
        or not 0 <= budget.cleanup_sink_keys <= 1000 * budget.cleanup_seconds)
    if invalid_export or actual_ops > budget.export_operations or actual_bytes > budget.export_bytes:
        ctx.issue('evidence_export_budget_gap', 'bounded export exceeds approved finite total')
    closes = len(ctx.kind(p1.KINDS[4]))
    if budget.close_transactions < 2 * closes or budget.close_rows < 7 * closes or budget.close_elapsed_us > 2000000 or budget.handoff_elapsed_us > 2000000 or budget.close_elapsed_us + budget.handoff_elapsed_us > min(4000000, budget.grace_us):
        ctx.issue('close_handoff_budget_gap', 'separate final/close-result transaction budget')
    return peaks
