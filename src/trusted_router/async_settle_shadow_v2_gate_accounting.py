"""Receipt, writer, deployment and authorization reconciliation for P5."""
from __future__ import annotations

from collections import Counter
from typing import Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_reconciliation as joins
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_contract as contract
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_bundle import Context, subject
from trusted_router.async_settle_shadow_v2_gate_result import Dimensions, Subject
from trusted_router.async_settle_shadow_v2_gate_verifiers import invoke
from trusted_router.async_settle_shadow_v2_schema import day_at, require, sha256
from trusted_router.detached_jws import canonical

PREREQUISITES = ('transport', 'size', 'regional_load', 'boot_transport', 'observer_freshness', 'marker_keys')


def deployment(ctx: Context) -> None:
    inv = ctx.bundle.inventory
    if inv is None:
        ctx.issue('writer_roster_gap', 'independent inventory absent')
        return
    if len({b.boot for b in inv.boots}) != len(inv.boots):
        ctx.issue('writer_roster_gap', 'duplicate independent boot')
    manifests = {v['day']: v for v in ctx.kind(p1.KINDS[5]) if 'router_writers' in v}
    counters = {(v['day'], v['boot']): v for v in ctx.kind(p1.KINDS[3])}
    closures = {(v['day'], v['boot']): v for v in ctx.kind(p1.KINDS[4])}
    expected: set[tuple[str, str]] = set()
    for day in sorted(set(ctx.bundle.days) | set(manifests)):
        manifest = manifests.get(day)
        if manifest is None:
            ctx.issue('writer_roster_gap', 'missing day manifest', Subject(day=day))
            continue
        for role in ('router', 'enclave'):
            roster = manifest[role + '_writers']
            boots = {b.boot: b for b in inv.boots if b.role == role and day_at(b.start_us) <= day
                     and (b.end_us is None or day <= day_at(max(b.start_us, b.end_us - 1)))}
            if set(manifest['revisions'][role]) != {v['revision'] for v in roster.values()}:
                ctx.issue('writer_roster_gap', 'revision roster differs', Subject(day=day))
            if set(roster) != set(boots) or not roster:
                ctx.issue('writer_roster_gap', 'manifest and inventory differ', Subject(day=day), tuple(sorted(set(roster) ^ set(boots))))
            if ctx.proof is None or ctx.proof['rosters_by_day'].get(day, {}).get(role) != roster:
                ctx.issue('writer_roster_gap', 'proof roster differs', Subject(day=day))
            for boot, info in roster.items():
                b = boots.get(boot)
                if b is None or info['revision'] != b.revision or info['region'] != b.region:
                    ctx.issue('writer_roster_gap', 'boot metadata differs', Subject(day, boot))
                if role == 'router':
                    expected.add((day, boot))
                    c = counters.get((day, boot))
                    if c is None or (day, boot) not in closures:
                        ctx.issue('writer_roster_gap', 'counter or closure absent', Subject(day, boot))
                    if c is not None and (c['started_at_us'], c['flushed_at_us']) != (info['lifetime']['start_us'], info['lifetime']['end_us']):
                        ctx.issue('writer_roster_gap', 'counter does not cover inventoried interval', Subject(day, boot))
                    if c is not None and (b is None or c['region'] != b.region):
                        ctx.issue('legacy_region_label', 'counter region differs', Subject(day, boot))
                    regions = ctx.proof['instance_regions_by_day'].get(day) if ctx.proof else None
                    if not regions or regions.get(boot) != info['region'] or set(regions) != set(manifest['router_writers']):
                        ctx.issue('regional_roster_unknown', 'incomplete per-boot region map', Subject(day, boot))
                intervals = sorted((v['interval']['start_us'], v['interval']['end_us']) for v in manifest['disabled_flag_intervals'])
                covered = info['lifetime']['start_us']
                for left, right in intervals:
                    if left <= covered:
                        covered = max(covered, right)
                if covered < info['lifetime']['end_us']:
                    ctx.issue('flag_interval_gap', 'flags do not cover lifetime', Subject(day, boot))
    for day, boot in set(counters) | set(closures):
        if (day, boot) not in expected:
            ctx.issue('writer_roster_gap', 'unexpected writer/day', Subject(day, boot))
    for s in ctx.kind(p1.KINDS[0]):
        d = s['deployment']
        b = next((b for b in inv.boots if b.boot == d['instance']), None)
        if b is None or d['router_revision'] != b.revision or not b.start_us <= s['observed_at_us'] < (b.end_us or ctx.bundle.completed_at_us):
            ctx.issue('writer_roster_gap', 'sample outside inventoried revision/lifetime', Subject(day_at(s['observed_at_us']), d['instance'], object_id=s['authorization_id']))
        if b is None or b.region != d['region']:
            ctx.issue('legacy_sample_region', 'sample region differs', Subject(day_at(s['observed_at_us']), d['instance'], object_id=s['authorization_id']))
    previous = None
    seen = set()
    for ledger in ctx.ledgers:
        boot = ledger['boot']
        b = next((b for b in inv.boots if b.boot == boot), None)
        if (boot in seen or ledger['previous_ledger_sha256'] != previous or b is None
                or any(ledger[k] != getattr(b, k) for k in ('role', 'revision', 'region'))
                or ledger['boot_at_us'] != b.start_us):
            ctx.issue('boot_prerequisite_gap', 'ledger identity or chain differs', Subject(boot=boot))
        seen.add(boot)
        previous = sha256(ledger)
    for boot in inv.boots:
        if boot.boot not in seen:
            ctx.issue('boot_prerequisite_gap', 'missing boot ledger', Subject(boot=boot.boot))
    for page, event in ctx.events():
        writer = next((b for b in inv.boots if b.boot == page['boot'] and b.role == 'router'), None)
        if writer is None or event['router_revision'] != writer.revision or not writer.start_us <= event['event_at_us'] < (writer.end_us or ctx.bundle.completed_at_us):
            ctx.issue('writer_roster_gap', 'receipt outside inventoried revision/lifetime', subject(page, event['seq']))
        if event['phase'] not in ('authorize', 'replay', 'settle', 'refund'):
            continue
        traffic_ledger = next((v for v in ctx.ledgers if v['boot'] == page['boot']), None)
        if traffic_ledger is None or event['event_at_us'] < max(traffic_ledger['traffic_ready_at_us'], *(traffic_ledger[k]['completed_at_us'] for k in PREREQUISITES)):
            ctx.issue('boot_prerequisite_gap', 'traffic before boot readiness', subject(page, event['seq']))


def receipt_accounting(ctx: Context) -> None:
    events = ctx.events()
    samples = {s['authorization_day'] + '/' + s['authorization_id']: s for s in ctx.kind(p1.KINDS[0])}
    for counter in ctx.kind(p1.KINDS[3]):
        sub = subject(counter)
        own = [(p, e) for p, e in events if (p['day'], p['boot']) == (counter['day'], counter['boot'])]
        terminal = [e for _, e in own if e['phase'] in ('settle', 'refund')]
        authorized = [e for _, e in own if e['phase'] in ('authorize', 'replay')]
        if len(authorized) != counter['authorize_attempts'] or sum(e['phase'] == 'authorize' for e in authorized) != counter['authorize_fresh']:
            ctx.issue('authorize_coverage_gap', 'fresh/replay receipt count differs', sub, tuple(e['seq'] for e in authorized))
        if len(terminal) != counter['settle_attempts'] + counter['refund_attempts']:
            ctx.issue('terminal_coverage_gap', 'terminal receipts incomplete (complete observed receipt list)', sub, tuple(e['seq'] for e in terminal),
                      count=abs(len(terminal) - counter['settle_attempts'] - counter['refund_attempts']))
        # The assertion population is the union, including declared zero buckets.
        buckets = {canonical([b['phase'], b['dimensions']]): (b['phase'], b['dimensions'], b)
                   for b in counter['buckets']}
        for _, event in own:
            phase = 'authorize' if event['phase'] == 'replay' else event['phase']
            if phase in ('authorize', 'settle', 'refund'):
                buckets.setdefault(canonical([phase, event['dimensions']]), (phase, event['dimensions'], None))
        for phase, dim, bucket in buckets.values():
            selected = [e for _, e in own if ('authorize' if e['phase'] == 'replay' else e['phase']) == phase and e['dimensions'] == dim]
            bs = Subject(counter['day'], counter['boot'], phase, Dimensions(**dim))
            expected_count = None if bucket is None else bucket['authorize_attempts' if phase == 'authorize' else phase + '_attempts']
            if len(selected) != expected_count:
                ctx.issue('phase_coverage_gap', 'phase/dimension receipts differ', sub)
            if phase == 'authorize':
                continue
            counts = Counter(e['partition'] for e in selected)
            if bucket is None or any(counts[k] != bucket[k] for k in ('A', 'B', 'C')) or counts['C'] > 0:
                ctx.issue('observed_partition_gap', 'terminal partition is not closed', bs)
            eligible = None if any(e['eligibility'] == 'unknown' for e in selected) else sum(e['eligibility'] == 'eligible' for e in selected)
            predictions = Counter(e['prediction']['prediction'] for e in selected if e['prediction'] is not None)
            y, n, u = (predictions[k] for k in ('yes', 'no', 'unknown'))
            if eligible is None or bucket is None or eligible != bucket['eligible_demand'] or y + n + u != eligible or eligible > 0 and y + n == 0:
                ctx.issue('prediction_demand_gap', 'predictions do not cover eligible demand', bs)
            if eligible is None or 100 * u > 2 * eligible:
                ctx.issue('prediction_unknown_gap', 'unknown prediction allowance exceeded', bs)
        eligible = None if any(e['eligibility'] == 'unknown' for e in terminal) else sum(e['eligibility'] == 'eligible' for e in terminal)
        if eligible is None or eligible != counter['eligible_demand']:
            ctx.issue('eligible_coverage_gap', 'independent eligible demand differs', sub)
        predictions = Counter(e['prediction']['prediction'] for e in terminal if e['prediction'] is not None)
        if any(predictions[k] != counter['admission_observer']['prediction_' + k] for k in ('yes', 'no', 'unknown')):
            ctx.issue('phase_coverage_gap', 'retained prediction totals differ from receipts', sub)
        for event in terminal:
            key = event['authorization_day'] + '/' + event['authorization_id']
            sample = samples.get(key)
            es = Subject(counter['day'], counter['boot'], event['phase'], Dimensions(**event['dimensions']), event['seq'])
            if event['partition'] == 'A':
                if (sample is None or key not in ctx.verified or ctx.ports is None
                        or not ctx.ports.comparator_positive(sample)
                        or sample['payload_hash'] != event['payload_sha256']
                        or sample['booking']['attempted_kind'] != event['phase']):
                    ctx.issue('eligible_coverage_gap', 'missing verified compared original', es)
            if event['partition'] == 'B':
                ctx.issue('policy_version_gap', 'eligible-only cohort has excluded traffic',
                          Subject(wire=a2.E(counter['day'], counter['boot'],
                              next(p['first_seq'] for p, e in own if e is event), event['seq'], event=event)))
                if event['proof_kind'] == 'certificate':
                    valid = key in ctx.verified
                else:
                    valid = bool(sample is not None and key in ctx.verified and ctx.ports is not None
                                 and ctx.ports.known_exclusion(sample, allow_unknown=True))
                if not valid:
                    ctx.issue('exclusion_proof_gap', 'unbacked exclusion', es)
    for page, event in events:
        if event['phase'] == 'authorize':
            try:
                facts = event['facts']
                require(facts is not None, 'authorization_link_gap')
                decision = contract.decide(contract.RequestedFacts.from_object(facts), event['decision']['reason'],
                    canonical(event['candidates']) if event['candidates'] is not None else None,
                    complete_candidates=event['candidates'] is not None)
                require(decision.eligibility == event['eligibility'] and decision.eligibility != 'unknown', 'authorization_link_gap')
                require(all(getattr(decision, k) == event['decision'][k] for k in ('reason', 'facts_sha256', 'candidate_set_sha256')), 'authorization_link_gap')
            except ValueError as exc:
                ctx.issue('authorization_link_gap', str(exc), subject(page, event['seq']))


def authorization(ctx: Context) -> None:
    events = ctx.events()
    supplied = [e for p in ctx.attached('authorization_obligation') for e in p['entries']]
    expected_locs = []
    for page, event in events:
        if event['phase'] not in ('authorize', 'replay'):
            continue
        sub = subject(page, event['seq'])
        options: dict[str, Any] = {}
        if event['phase'] == 'replay':
            reference = event['retry_original']
            original = next(((p, e) for p, e in events if reference is not None and
                (p['day'], p['boot'], e['seq']) == (reference['day'], reference['boot'], reference['event_seq'])), None)
            if original:
                options = dict(original_page=original[0], original_event_seq=original[1]['seq'])
        try:
            obligation = joins.authorization_obligation(page, event['seq'], **options)
            expected_locs.append(obligation['original_receipt'])
            matches = [o for o in supplied if o['original_receipt'] == obligation['original_receipt']]
            if len(matches) != 1:
                ctx.issue('authorization_obligation_gap', 'missing/duplicated report obligation', sub)
            candidates = [(p, e) for p, e in events if e['phase'] in ('settle', 'refund') and e['authorization_id'] == event['authorization_id']]
            if not candidates:
                ctx.issue('authorization_horizon_gap', 'no durable terminal in original horizon', sub)
            valid = []
            for tp, terminal in candidates:
                try:
                    locator = joins.event_locator(tp, terminal['seq'])
                    joins.validate_terminal_join(obligation, page, tp, locator, **options)
                    valid.append(locator)
                except ValueError as exc:
                    gap = str(exc) if str(exc) in ('authorization_link_gap', 'authorization_terminal_conflict', 'authorization_horizon_gap') else 'authorization_link_gap'
                    ctx.issue(gap, str(exc), sub, (tp['day'] + '/' + tp['boot'] + '/' + terminal['seq'],))
            if valid:
                obligation = {**obligation, 'status': 'terminal_reconciled', 'terminal_reference': valid[0]}
            if matches and matches[0] != obligation:
                ctx.issue('authorization_obligation_gap', 'report projection differs from actual terminal join', sub)
            ctx.obligations.append(obligation)
        except ValueError as exc:
            ctx.issue('authorization_link_gap', str(exc), sub)
    extras = [o for o in supplied if o['original_receipt'] not in expected_locs]
    for o in extras:
        ctx.issue('authorization_obligation_gap', 'unreferenced obligation', subject(o['original_receipt']))
    for closure in ctx.kind(p1.KINDS[4]):
        own = [o for o in ctx.obligations if all(o['original_receipt'][k] == closure[k] for k in ('day', 'boot'))]
        try:
            joins.obligation_projection(closure, own)
        except ValueError as exc:
            ctx.issue('authorization_obligation_gap', str(exc), subject(closure))


def durability(ctx: Context) -> None:
    events = ctx.events()
    for counter in ctx.kind(p1.KINDS[3]):
        sub = subject(counter)
        pages = sorted((p for p in ctx.kind(p1.KINDS[2]) if all(p[k] == counter[k] for k in ('day', 'boot'))), key=lambda p: p['first_seq'])
        previous = None
        sequence = 1
        for page in pages:
            if page['previous_page_sha256'] != previous or int(page['first_seq']) != sequence:
                ctx.issue('checkpoint_reconciliation_gap', 'receipt chain hole', sub)
            previous = sha256(page)
            sequence = int(page['last_seq']) + 1
        if sequence - 1 != counter['event_seq'] or counter['durable_event_hwm'] != counter['event_seq'] or counter['receipt_count'] != sequence - 1 or previous != counter['receipt_chain_sha256']:
            ctx.issue('checkpoint_reconciliation_gap', 'HWM or receipt chain differs', sub)
        if counter['loss_count'] or counter['irrecoverable_losses'] or counter['unreconciled_commits'] or counter['comparison_dropped'] or counter['counter_overflow'] or counter['dimension_overflow']:
            ctx.issue('evidence_loss_gap', 'residual evidence loss', sub)
        closure = next((c for c in ctx.kind(p1.KINDS[4]) if all(c[k] == counter[k] for k in ('day', 'boot'))), None)
        if closure is None or not counter['closed']:
            ctx.issue('writer_closure_gap', 'writer is not closed', sub)
            continue
        result = next((r for r in ctx.kind(a1.KINDS[-1]) if all(r[k] == counter[k] for k in ('day', 'boot'))), None)
        boot = next((b for b in ctx.bundle.inventory.boots if b.boot == counter['boot']), None) if ctx.bundle.inventory else None
        if result is None or boot is None or boot.end_us is None:
            ctx.issue('close_result_identity_gap', 'original boot close result absent', sub)
        else:
            try:
                a1.validate_close_result(result, closure, termination_applies=boot.termination_applies)
                a1.sealed_closure(result, closure, counter, lifecycle={boot.boot: dict(revision=boot.revision, region=boot.region,
                    lifetime=dict(start_us=boot.start_us, end_us=boot.end_us))}, termination_applies=boot.termination_applies)
            except ValueError as exc:
                ctx.issue('writer_closure_gap' if str(exc) == 'writer_closure_gap' else 'close_result_identity_gap', str(exc), sub)
        handoffs = [h for h in ctx.attached('handoff_accounting') if all(h[k] == counter[k] for k in ('day', 'boot'))]
        if len(handoffs) != 1 or result is None or handoffs[0]['result_sha256'] != sha256(result) or not handoffs[0]['landed']:
            ctx.issue('handoff_coverage_gap', 'missing conservative handoff accounting', sub)
    for page, event in events:
        ref = event.get('retry_original')
        if event['phase'] == 'completion' and ref is None:
            ref = {'day': page['day'], 'boot': page['boot'], 'event_seq': event['completion_of']}
        if ref is None:
            continue
        original = next(((p, e) for p, e in events if (p['day'], p['boot'], e['seq']) == (ref['day'], ref['boot'], ref['event_seq'])), None)
        try:
            require(original is not None, 'retry_original_gap')
            assert original is not None
            op, oe = original
            if event['phase'] == 'completion':
                joins.validate_completion(op, joins.event_locator(op, oe['seq']), page, joins.event_locator(page, event['seq']))
            elif event['phase'] != 'replay':
                key = event['authorization_day'] + '/' + event['authorization_id']
                signature = next((s for s in ctx.bundle.signatures if s.object_id == key), None)
                if event['proof_kind'] != 'certificate':
                    require(signature is not None and ctx.ports is not None, 'retry_original_gap')
                    assert signature is not None and ctx.ports is not None
                    invoke(ctx, 'binding', signature.token, signature.keys, event['receipt_at_us'] // 1000000)
                require(oe['phase'] == event['phase'] and oe['receipt_at_us'] <= event['event_at_us'] and
                    all(oe[k] == event[k] for k in ('identity', 'decision', 'dimensions', 'payload_sha256', 'certificate_sha256', 'proof_kind')), 'retry_original_gap')
        except ValueError as exc:
            ctx.issue('retry_original_gap', str(exc), subject(page, event['seq']))
