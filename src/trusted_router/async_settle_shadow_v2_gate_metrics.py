"""A2 metric populations; no counter-to-timing reconstruction or null coercion."""
from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_schema import integer

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context


def total(values: Iterable[int | None]) -> int | None:
    items = list(values)
    if None in items:
        return None
    result = sum(v for v in items if v is not None)
    integer(result)
    return result


def stat(values: Iterable[int | None] | None) -> dict[str, Any]:
    if values is None:
        return dict.fromkeys(a2.STAT.fields)
    items = list(values)
    known = sorted(v for v in items if v is not None)
    result = dict(count=len(known), null_count=len(items) - len(known),
                  min=known[0] if known else None, max=known[-1] if known else None)
    for name, percent in (('p50', 50), ('p95', 95), ('p99', 99)):
        result[name] = known[(len(known) * percent + 99) // 100 - 1] if known else None
    a2.STAT(result)
    return result


def delta_bin(value: int) -> str:
    if value == 0:
        return '0'
    size = abs(value)
    suffix = '1' if size == 1 else '2-10' if size <= 10 else '11-100' if size <= 100 else '101-1000' if size <= 1000 else '>1000'
    return ('+' if value > 0 else '-') + suffix


def deltas(values: Iterable[int | None] | None) -> dict[str, Any]:
    if values is None:
        return dict(count=None, null_count=None, min_micro=None, max_micro=None, bins=dict.fromkeys(a2.BINS))
    items = list(values)
    known = [v for v in items if v is not None]
    bins = dict.fromkeys(a2.BINS, 0)
    for value in known:
        bins[delta_bin(value)] += 1
    result = dict(count=len(known), null_count=len(items) - len(known),
        min_micro=min(known) if known else None, max_micro=max(known) if known else None, bins=bins)
    a2.DELTAS(result)
    assert sum(bins.values()) == result['count']
    return result


def ratio(numerator: int | None, denominator: int | None, *, required: bool, closed_empty: bool) -> dict[str, Any]:
    if not required:
        state = 'not_required'
    elif numerator is None or denominator is None or numerator > denominator:
        state = 'unevaluable'
    elif denominator > 0:
        state = 'evaluated'
    elif numerator == 0 and closed_empty:
        state = 'not_exercised'
    else:
        state = 'unevaluable'
    result = dict(state=state, numerator=numerator if state in ('evaluated', 'not_exercised') else None,
                  denominator=denominator if state in ('evaluated', 'not_exercised') else None)
    a2.RATIO(result)
    return result


def observations(*, samples: list[dict[str, Any]] | None, terminals: list[dict[str, Any]] | None,
                 slots: list[dict[str, Any]] | None, closes: list[dict[str, Any]] | None,
                 handoffs: list[dict[str, Any]] | None, terminations: list[dict[str, Any]] | None) -> dict[str, Any]:
    timing = {}
    for name in a2.TIMINGS[:8]:
        parent = 'admission' if name in ('workspace_age_us', 'health_age_us') else 'timing'
        timing[name] = stat(None if samples is None else [s[parent][name] for s in samples])
    for name, field in (('publisher_cycle_us', 'cycle_us'), ('publisher_slot_rpc_elapsed_us', 'rpc_elapsed_us')):
        timing[name] = stat(None if slots is None else [s[field] for s in slots])
    for name, items, field in (('close_us', closes, 'elapsed_us'), ('handoff_us', handoffs, 'result_us'),
                               ('drain_us', terminations, 'final_pass_us')):
        timing[name] = stat(None if items is None else [s[field] for s in items])
    delta = deltas(None if samples is None else [s['booked_minus_frozen'] for s in samples])
    nulls = dict(eligibility=None if samples is None else sum(s['eligibility']['observed'] is None for s in samples),
        booking=None if samples is None else sum(s['booked_micro'] is None for s in samples),
        comparison=None if terminals is None else sum(t['comparison'] is None for t in terminals),
        prediction=None if terminals is None else sum(t['prediction'] is None for t in terminals),
        timing=total(s['null_count'] for s in timing.values()), delta=delta['null_count'])
    if samples is not None:
        assert all(timing[k]['count'] + timing[k]['null_count'] == len(samples) for k in a2.TIMINGS[:8])
        assert delta['count'] + delta['null_count'] == len(samples)
        assert timing['evidence_write_us']['count'] == 0
    if slots is not None:
        assert all(timing[k]['count'] + timing[k]['null_count'] == len(slots) for k in a2.TIMINGS[8:10])
        assert timing['publisher_slot_rpc_elapsed_us']['null_count'] == 0
    return dict(timing=timing, deltas=delta, null_counts=nulls)


def counts(ctx: Context, counters: list[dict[str, Any]] | None, events: list[dict[str, Any]] | None,
           required: tuple[str, ...], *, enclave: bool = False) -> dict[str, Any]:
    available = counters is not None and events is not None
    terminal = [ev for ev in (events or []) if ev['phase'] in ('settle', 'refund')]
    def counter(field: str, rule: str) -> int | None:
        return total(c[field] for c in counters or []) if available and rule in required else None
    partition = {k: counter(k, 'G4') for k in ('A', 'B', 'C')}
    # Actual fleet-budgets requires G5's closed partition even though G4 itself
    # is not a required whole-stage rule.
    if available and 'G5' in required:
        partition = {k: total(c[k] for c in counters or []) for k in ('A', 'B', 'C')}
    partition['T'] = total(partition.values())
    eligible = None if not available or any(ev['eligibility'] == 'unknown' for ev in terminal) else sum(ev['eligibility'] == 'eligible' for ev in terminal)
    admission: dict[str, Any] = dict(E=eligible if 'G5' in required and not enclave else None)
    for code, word in (('Y', 'yes'), ('N', 'no'), ('U', 'unknown')):
        admission[code] = total(c['admission_observer']['prediction_' + word] for c in counters or []) if available and 'G5' in required and not enclave else None
    closed = bool(available and partition['C'] == 0 and all(admission[k] == 0 for k in ('E', 'Y', 'N', 'U')))
    admission['coverage'] = ratio(total(admission[k] for k in ('Y', 'N')), admission['E'], required='G5' in required and not enclave, closed_empty=closed)
    comparator: dict[str, Any] = dict(eligible=admission['E'], compared=partition['A'] if not enclave else None)
    for name, classes in (('exact', ('exact',)), ('explained', ('explained-by-catalog-change',)),
                          ('mismatch', ('mismatch',)), ('unevaluable', (None, 'unevaluable'))):
        comparator[name] = sum(t['comparison'] in classes for t in terminal if t['eligibility'] == 'eligible') if available and 'G5' in required and not enclave else None
    comparator['coverage'] = ratio(comparator['compared'], comparator['eligible'], required='G5' in required and not enclave, closed_empty=closed)
    attempts = {name: (sum(ev['phase'] == phase for ev in events or []) if available and ('G3' in required or 'G5' in required) else None)
                for name, phase in (('authorize_fresh', 'authorize'), ('authorize_replay', 'replay'), ('completion', 'completion'))}
    for name, retry in (('terminal_original', False), ('terminal_retry', True)):
        attempts[name] = sum((t['retry_original'] is not None) == retry for t in terminal) if available and ('G3' in required or 'G5' in required) else None
    checkpoint = {name: counter(source, 'G8') for name, source in (
        ('attempts', 'checkpoint_attempts'), ('failures', 'checkpoint_failures'), ('recoveries', 'checkpoint_recoveries'),
        ('unknown_commits', 'unknown_commits'), ('unreconciled_commits', 'unreconciled_commits'),
        ('irrecoverable_losses', 'irrecoverable_losses'), ('evidence_loss', 'loss_count'))}
    return dict(partition=partition, admission=admission, comparator=comparator, attempts=attempts, checkpoint=checkpoint)
