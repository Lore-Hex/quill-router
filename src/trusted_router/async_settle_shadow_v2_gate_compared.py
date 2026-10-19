"""D1 content-free router outcomes and their original receipt joins."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a1_reconciliation as joins
from trusted_router import async_settle_shadow_v2_contract as policy
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_bundle import Context, subject
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical


def sample_terminal(ctx: Context, sample: dict[str, Any]) -> None:
    reference = sample['original_terminal_receipt']
    matches = [(page, event) for page, event in ctx.events() if
        (page['day'], page['boot'], event['seq']) ==
        (reference['day'], reference['boot'], reference['event_seq'])]
    require(len(matches) == 1, 'eligible_coverage_gap')
    page, event = matches[0]
    require(event.get('retry_original') is None, 'retry_original_gap')
    require(sample['deployment']['instance'] == page['boot']
        and sample['deployment']['router_revision'] == event['router_revision']
        and sample['observed_at_us'] == event['event_at_us'], 'identity')
    compared_terminal(ctx, page, event, sample)


def compared_terminal(ctx: Context, page: dict[str, Any], event: dict[str, Any], sample: dict[str, Any]) -> None:
    """Never infer hidden claims from hashes; check only retained frozen fields."""
    require(ctx.ports is not None and ctx.ports.comparator_positive(sample), 'eligible_coverage_gap')
    require(event['phase'] in ('settle', 'refund') and event['partition'] == 'A'
        and event['eligibility'] == 'eligible' and event['proof_kind'] == sample['proof_kind'] == 'compared'
        and event['comparison'] == sample['classification'], 'eligible_coverage_gap')
    require(all(event[k] == sample[k] for k in ('authorization_day', 'authorization_id'))
        and event['payload_sha256'] == sample['payload_hash'] and sample['snapshot_hash'] is not None,
        'identity')
    require(event['booking']['winner'] == event['phase'] == sample['booking']['attempted_kind']
        and event['booking']['outcome'] == sample['booking']['outcome'], 'identity')
    require(all(event['dimensions'][k] == sample[k] for k in ('adapter', 'route_type', 'streamed')), 'identity')
    # Each retry has its own prediction. Its schema and the accounting pass
    # reconcile that attempt; it need not equal the first attempt's prediction.
    require(event['prediction'] is not None, 'prediction_demand_gap')
    reference = event.get('retry_original') or dict(day=page['day'], boot=page['boot'], event_seq=event['seq'])
    require(reference == sample['original_terminal_receipt'], 'retry_original_gap')
    issued = [(p, e) for p, e in ctx.events() if e['phase'] == 'authorize'
        and e['authorization_id'] == event['authorization_id'] and e['authorization_day'] == event['authorization_day']]
    require(len(issued) == 1, 'authorization_link_gap')
    ip, ie = issued[0]
    obligation = joins.authorization_obligation(ip, ie['seq'])
    joins.validate_terminal_join(obligation, ip, page, joins.event_locator(page, event['seq']))
    require(sample['authorize_at_us'] == ie['event_at_us'], 'authorization_link_gap')
    decision = policy.decide(policy.RequestedFacts.from_object(ie['facts']), ie['decision']['reason'],
        canonical(ie['candidates']) if ie['candidates'] is not None else None,
        complete_candidates=ie['candidates'] is not None)
    require(decision.eligibility == 'eligible' and all(getattr(decision, k) == ie['decision'][k]
        for k in ('reason', 'facts_sha256', 'candidate_set_sha256')), 'authorization_link_gap')


def reject_terminal(ctx: Context, page: dict[str, Any], event: dict[str, Any]) -> None:
    """Missing original support remains a failure in every dependent rule."""
    sub = subject(page, event['seq'])
    for gap in ('terminal_coverage_gap', 'eligible_coverage_gap', 'evidence_loss_gap', 'missing_support'):
        ctx.issue(gap, 'compared outcome lacks matching original evidence', sub)
    for ip, ie in ctx.events():
        if ie['phase'] in ('authorize', 'replay') and ie['authorization_id'] == event['authorization_id']:
            ctx.issue('authorization_terminal_conflict', 'compared terminal is not reconciled', subject(ip, ie['seq']))
    if event.get('retry_original') is not None:
        ctx.issue('retry_original_gap', 'retry outcome differs from its original', sub)


def verify_terminals(ctx: Context) -> None:
    samples = {s['authorization_day'] + '/' + s['authorization_id']: s for s in ctx.kind(p1.KINDS[0])}
    for page, event in ctx.events():
        if event['phase'] not in ('settle', 'refund') or not (event.get('proof_kind') == 'compared' or event.get('retry_original')):
            continue
        key = event['authorization_day'] + '/' + event['authorization_id']
        try:
            require(key in ctx.verified and key in samples, 'eligible_coverage_gap')
            compared_terminal(ctx, page, event, samples[key])
        except ValueError:
            reject_terminal(ctx, page, event)
