"""Expected populations from approved lifetimes, commitments and references."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a1_health as health
from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_inputs import Key
from trusted_router.async_settle_shadow_v2_gate_loading import error_detail
from trusted_router.async_settle_shadow_v2_gate_result import Subject
from trusted_router.async_settle_shadow_v2_schema import day_at, sha256

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context

DAY = 86400000000


def writers(ctx: Context, manifest: dict[str, Any]) -> set[str]:
    expected = set(manifest['router_writers'])
    if ctx.bundle.inventory:
        expected.update(b.boot for b in ctx.bundle.inventory.boots if b.role == 'router'
            and day_at(b.start_us) <= manifest['day'] <= day_at((b.end_us or ctx.bundle.completed_at_us) - 1))
    return expected


def receipt_keys(ctx: Context, manifest: dict[str, Any]) -> set[str]:
    expected: set[str] = set()
    for boot in writers(ctx, manifest):
        counter = ctx.rows.get(Key(p1.KINDS[3], manifest['day'] + '/' + boot))
        if counter is None:
            continue
        first = 1
        previous = None
        while first <= counter['durable_event_hwm']:
            key = manifest['day'] + '/' + boot + '/' + f'{first:020}'
            expected.add(key)
            page = ctx.rows.get(Key(p1.KINDS[2], key))
            if page is None or page['previous_page_sha256'] != previous:
                break
            next_first = int(page['last_seq']) + 1
            if next_first <= first or next_first > counter['durable_event_hwm'] + 1:
                break
            first, previous = next_first, sha256(page)
    return expected


def trace_keys(ctx: Context) -> set[str]:
    """First scheduled slot then verified predecessor; never observed-key union."""
    cache_key = (tuple(row for row in ctx.bundle.attachments if row.key.kind == 'publisher_trace'),
                 ctx.bundle.inventory, ctx.bundle.completed_at_us)
    if ctx.trace_population_cache is not None and ctx.trace_population_cache[0] == cache_key:
        return set(ctx.trace_population_cache[1])
    expected: set[str] = set()
    pages: dict[str, dict[str, Any]] = {}
    conflicts = set()
    for row in ctx.bundle.attachments:
        if row.key.kind != 'publisher_trace':
            continue
        try:
            page = health.PUBLISHER_TRACE_PAGE.parse(row.raw)
        except ValueError:
            continue
        key = 'publisher-traces/' + page['day'] + '/' + page['boot'] + '/' + str(page['first_slot'])
        if row.key.id != key or key in pages and pages[key] != page:
            conflicts.add(key)
        else:
            pages[key] = page
    if ctx.bundle.inventory is None:
        return expected
    for boot in ctx.bundle.inventory.boots:
        if not boot.contender or boot.role != 'router':
            continue
        end = boot.end_us or ctx.bundle.completed_at_us
        for midnight in range(boot.start_us // DAY * DAY, end, DAY):
            left, right = max(boot.start_us, midnight), min(end, midnight + DAY)
            first, last = (left + 1999999) // 2000000, (right - 1) // 2000000
            previous = None
            while first <= last:
                key = 'publisher-traces/' + day_at(left) + '/' + boot.boot + '/' + str(first)
                expected.add(key)
                found = pages.get(key)
                if found is None or key in conflicts or found['previous_sha256'] != previous:
                    break
                next_first = found['last_slot'] + 1
                if not first < next_first <= last + 1:
                    break
                first, previous = next_first, sha256(found)
    ctx.trace_population_cache = (cache_key, frozenset(expected))
    return expected


def audit_expected(ctx: Context) -> None:
    manifests = {m['day']: m for m in ctx.manifests()}
    for day in ctx.bundle.days:
        if day not in manifests:
            ctx.issue('artifact_gap', 'expected day manifest absent', Subject(wire=a2.O(p1.KINDS[5], day + '/manifest-v2')))
    if ctx.bundle.inventory is not None:
        for day, manifest in manifests.items():
            for role in ('router', 'enclave'):
                independent = {b.boot: b for b in ctx.bundle.inventory.boots if b.role == role
                    and day_at(b.start_us) <= day <= day_at((b.end_us or ctx.bundle.completed_at_us) - 1)}
                roster = manifest[role + '_writers']
                if set(independent) != set(roster):
                    ctx.issue('writer_roster_gap', 'independent expected roster differs', Subject(day=day))
                for boot in set(independent) & set(roster):
                    source = independent[boot]
                    value = roster[boot]
                    if value['revision'] != source.revision or value['region'] != source.region:
                        ctx.issue('writer_roster_gap', 'independent revision/region differs', Subject(day=day))
    expected_rows: dict[Key, str] = {}
    expected_receipts = set()
    for day, manifest in manifests.items():
        for boot in writers(ctx, manifest):
            for kind, gap in ((p1.KINDS[3], 'phase_coverage_gap'), (p1.KINDS[4], 'writer_closure_gap'),
                              (a1.KINDS[-1], 'close_result_identity_gap')):
                expected_rows[Key(kind, day + '/' + boot)] = gap
        expected_receipts.update(receipt_keys(ctx, manifest))
    expected_rows.update({Key(p1.KINDS[2], key): 'authorize_coverage_gap' for key in expected_receipts})
    expected_traces = trace_keys(ctx)
    expected_rows.update({Key('publisher_trace', key): 'publisher_trace_gap' for key in expected_traces})
    available = ctx.rows.keys() | ctx.attachments.keys()
    for key, gap in expected_rows.items():
        if key not in available:
            sub = Subject(wire=a2.O(key.kind, key.id))
            ctx.issue('artifact_gap', 'independently expected source absent', sub)
            parts = key.id.split('/')
            ctx.issue(gap, 'independently expected source absent', sub if gap == 'close_result_identity_gap'
                else Subject(day=parts[-3] if key.kind == 'publisher_trace' else parts[0],
                             boot=parts[-2] if key.kind == 'publisher_trace' else parts[1]))
    for key in available:
        if key.kind in (p1.KINDS[2], p1.KINDS[3], p1.KINDS[4], a1.KINDS[-1], 'publisher_trace') and key not in expected_rows:
            ctx.issue('evidence_schema_gap', 'unexpected source outside expected population', Subject(wire=a2.O(key.kind, key.id)))
    for counter in ctx.kind(p1.KINDS[3]):
        day, boot = counter['day'], counter['boot']
        pages = sorted((page for page in ctx.kind(p1.KINDS[2]) if (page['day'], page['boot']) == (day, boot)), key=lambda p: p['first_seq'])
        previous = None
        first = 1
        broken = False
        for page in pages:
            broken |= page['previous_page_sha256'] != previous or int(page['first_seq']) != first
            first, previous = int(page['last_seq']) + 1, sha256(page)
        if broken or first - 1 != counter['durable_event_hwm'] or first - 1 != counter['receipt_count'] or previous != counter['receipt_chain_sha256']:
            ctx.issue('checkpoint_reconciliation_gap', 'committed receipt chain/HWM mismatch', Subject(day=day, boot=boot))
        closure = ctx.rows.get(Key(p1.KINDS[4], day + '/' + boot))
        result = ctx.rows.get(Key(a1.KINDS[-1], day + '/' + boot))
        lifetime = next((b for b in ctx.bundle.inventory.boots if b.boot == boot), None) if ctx.bundle.inventory else None
        if closure is not None and result is not None and lifetime is not None:
            try:
                a1.sealed_closure(result, closure, counter, lifecycle={boot: dict(revision=lifetime.revision,
                    region=lifetime.region, lifetime=dict(start_us=lifetime.start_us, end_us=lifetime.end_us))},
                    termination_applies=lifetime.termination_applies)
            except ValueError as exc:
                gap = 'writer_closure_gap' if error_detail(exc) == 'writer_closure_gap' else 'close_result_identity_gap'
                ctx.issue(gap, 'original close binding: ' + error_detail(exc),
                    Subject(wire=a2.O(a1.KINDS[-1], day + '/' + boot)) if gap == 'close_result_identity_gap' else Subject(day=day, boot=boot))
        handoffs = [h for h in ctx.attached('handoff_accounting') if (h['day'], h['boot']) == (day, boot)]
        if len(handoffs) != 1 or result is None or handoffs[0]['result_sha256'] != sha256(result):
            ctx.issue('handoff_coverage_gap', 'expected original handoff accounting absent or changed', Subject(day=day, boot=boot))
    # Frozen coverage pages commit exact attachment keys. Availability never
    # substitutes for this independent committed population.
    from trusted_router.async_settle_shadow_v2_gate_bundle import coverage_issues
    coverage_issues(ctx)
    for day, manifest in manifests.items():
        close_keys = {day + '/' + boot for boot in writers(ctx, manifest)}
        for stream, expected in (('close_result', close_keys), ('publisher_trace', {k for k in expected_traces if '/' + day + '/' in k})):
            actual = {e['key'] for p in ctx.attached('coverage') if p['day'] == day and p['stream'] == stream for e in p['entries']}
            if actual != expected:
                ctx.issue('artifact_gap', 'coverage expected-set mismatch: ' + stream, Subject(day=day, object_id=stream))
    for kind, order, gap in (('publisher_trace', 'first_slot', 'publisher_trace_gap'),
                             ('health_basis', 'sequence', 'shadow_health_gap')):
        batches: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for page in ctx.attached(kind):
            batches.setdefault((page['day'], page['boot']), []).append(page)
        for (day, boot), pages in batches.items():
            previous = None
            for index, page in enumerate(sorted(pages, key=lambda p: p[order]), 1):
                if page['previous_sha256'] != previous or kind == 'health_basis' and int(page['sequence']) != index:
                    ctx.issue(gap, 'original attachment predecessor chain mismatch', Subject(day=day, boot=boot))
                previous = sha256(page)
    # Reader references determine health demand even if every basis is deleted.
    bases = [b for page in ctx.attached('health_basis') for b in page['bases']]
    samples = [(s['deployment']['instance'], s['authorization_day'], s['health_slot']) for s in ctx.kind(p1.KINDS[0])]
    predictions = [(page['boot'], page['day'], e['prediction']['health_slot'])
                   for page, e in ctx.events() if e.get('prediction') is not None]
    for boot, day, slot in samples + predictions:
        if slot is not None and not any(b['reader_boot'] == boot and b['body'] is not None and b['body']['slot'] == slot for b in bases):
            ctx.issue('artifact_gap', 'referenced health basis absent', Subject(wire=a2.O('health_basis', a2.L(day, boot, str(slot)))))
            ctx.issue('shadow_health_gap', 'referenced health basis absent', Subject(day=day, boot=boot))
