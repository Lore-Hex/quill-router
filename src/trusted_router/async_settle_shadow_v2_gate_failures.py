"""A2 assertion identities and deterministic listings, using the contract table."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, replace
from hashlib import sha256 as identity_digest
from itertools import chain
from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_result import (
    Failure,
    FailureSet,
    FailureSummary,
    Subject,
)
from trusted_router.async_settle_shadow_v2_schema import day_at, integer, sha256
from trusted_router.detached_jws import canonical

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context

TAG = dict(v=2, policy_version='shadow-v2', contract_revision='a2')


def writer_keys(ctx: Context, sub: Subject) -> Iterable[tuple[str, str, str]]:
    """Union of independently keyed observed and expected writer days."""
    found: set[tuple[str, str, str]] = set()
    if ctx.bundle.inventory:
        for boot in ctx.bundle.inventory.boots:
            for day in ctx.bundle.days:
                if day_at(boot.start_us) <= day <= day_at((boot.end_us or ctx.bundle.completed_at_us) - 1):
                    found.add((day, boot.role, boot.boot))
    for kind in (p1.KINDS[2], p1.KINDS[3], p1.KINDS[4]):
        found.update((v['day'], 'router', v['boot']) for v in ctx.kind(kind))
    if sub.day and sub.boot:
        roles = {r for d, r, b in found if d == sub.day and b == sub.boot}
        if not roles:
            found.add((sub.day, 'router', sub.boot))
    return ((d, r, b) for d, r, b in sorted(found)
            if (sub.day is None or sub.day == d) and (sub.boot is None or sub.boot == b))


def writer_subjects(ctx: Context, sub: Subject) -> list[dict[str, Any]]:
    return [a2.W(*key) for key in writer_keys(ctx, sub)]


def fallback(values: Iterable[dict[str, Any]], missing: dict[str, Any] | None = None) -> Iterable[dict[str, Any]]:
    iterator = iter(values)
    first = next(iterator, None)
    return [a2.N() if missing is None else missing] if first is None else chain((first,), iterator)


def subjects(ctx: Context, row: a2.FailureRule, sub: Subject) -> Iterable[dict[str, Any]]:
    if sub.wire is not None:
        return [sub.wire]
    codes = row.constructor
    if codes == ('N',):
        return [a2.N()]
    if codes == ('D',):
        return [a2.D(sub.day)] if sub.day else [a2.D(d) for d in ctx.bundle.days] or [a2.N()]
    if codes == ('V',):
        if sub.object_id:
            return [a2.V(sub.object_id)]
        traffic = {p['boot'] for p, e in ctx.events() if e['phase'] in ('authorize', 'replay', 'settle', 'refund')}
        return [a2.V(r) for r in sorted({b.region for b in ctx.bundle.inventory.boots if b.role == 'router' and b.boot in traffic})] if ctx.bundle.inventory and traffic else [a2.N()]
    if codes == ('A',):
        return [a2.A('pre')]
    if codes == ('K',):
        stages = ('pre',) if ctx.gate == 'fleet-budgets-pre' else ('pre', 'post')
        if ctx.bundle.inventory is None or ctx.bundle.budget is None:
            return [a2.N()]
        regions = [sub.object_id] if sub.object_id else [None, *sorted({b.region for b in ctx.bundle.inventory.boots if b.role == 'router'})]
        return (a2.K(stage, region) for stage in stages for region in regions)
    if codes == ('P',):
        name = 'pilot' if row.name == 'pilot_prerequisite_gap' else sub.object_id
        return [a2.P(ctx.gate, name)] if name else [a2.P(ctx.gate, n) for n in ctx.bundle.external.required] if ctx.bundle.external and ctx.bundle.external.required else [a2.N()]
    if codes == ('Q',):
        return fallback(a2.Q(r) for r in ctx.reset_history if (r['reason'] == row.name or row.name == 'missing_support')
                and (sub.object_id is None or str(r['at_us']) == sub.object_id))
    if 'B' in codes:
        if sub.day and sub.boot and sub.phase in ('settle', 'refund') and sub.dimensions:
            return [a2.B(sub.day, sub.boot, sub.phase, asdict(sub.dimensions))]
        def buckets() -> Iterable[dict[str, Any]]:
            for counter in ctx.kind(p1.KINDS[3]):
                if (sub.day is None or counter['day'] == sub.day) and (sub.boot is None or counter['boot'] == sub.boot):
                    for bucket in counter['buckets']:
                        if bucket['phase'] in ('settle', 'refund'):
                            yield a2.B(counter['day'], counter['boot'], bucket['phase'], bucket['dimensions'])
            for page, event in ctx.events():
                if event['phase'] in ('settle', 'refund') and (sub.day is None or page['day'] == sub.day) and (sub.boot is None or page['boot'] == sub.boot):
                    yield a2.B(page['day'], page['boot'], event['phase'], event['dimensions'])
        return fallback(buckets())
    if 'W' in codes:
        return fallback(a2.W(d, r, b) for d, r, b in writer_keys(ctx, sub) if row.rule == 'G2' or r == 'router')
    if codes == ('E',):
        found = (a2.E(p['day'], p['boot'], p['first_seq'], ev['seq'], event=ev)
                 for p, ev in ctx.events() if (sub.day is None or p['day'] == sub.day)
                 and (sub.boot is None or p['boot'] == sub.boot) and (sub.object_id is None or ev['seq'] == sub.object_id)
                 and (ev['phase'] in ('authorize', 'replay') if row.rule == 'G3' else
                      ev['phase'] == 'completion' or ev.get('retry_original') is not None if row.name == 'retry_original_gap' else
                      ev['phase'] in ('settle', 'refund')))
        if sub.object_id:
            parts = sub.object_id.split('/')
            if len(parts) == 3 and (sub.day is None or parts[0] == sub.day) and (sub.boot is None or parts[1] == sub.boot):
                return fallback(found, a2.O(p1.KINDS[2], sub.object_id))
        return fallback(found)
    if row.name in ('close_result_identity_gap', 'close_result_conflict'):
        key = sub.day + '/' + sub.boot if sub.day and sub.boot else sub.object_id
        return [a2.O('async_settle_shadow_close_result_v2', key)] if key else [a2.N()]
    if sub.object_id:
        candidates = [r.key for r in (*ctx.bundle.rows, *ctx.bundle.attachments) if r.key.id == sub.object_id]
        if len(set(candidates)) == 1:
            return [a2.O(candidates[0].kind, candidates[0].id)]
        return [a2.O('a2_file', sub.object_id)]
    return [a2.N()]


def bounded(failures: Iterable[Failure]) -> FailureSet:
    """Tally distinct assertions with a digest/day-bit index and 16 wire subjects.

    Diagnostics never accumulate a full Failure collection. The identity index
    contains only 32-byte digests and affected-day bitsets, not subject objects.
    """
    seen: dict[tuple[str, bytes], int] = {}
    day_bits: dict[str, int] = {}
    counts: dict[str, int] = {}
    days: dict[str, dict[str, int]] = {}
    prefixes: dict[str, dict[bytes, Failure]] = {}
    for failure in failures:
        key = canonical(failure.subject)
        identity = (failure.gap, identity_digest(key).digest())
        mask = 0
        for day in failure.days:
            day_bits.setdefault(day, 1 << len(day_bits))
            mask |= day_bits[day]
        fresh = identity not in seen
        old = seen.get(identity, 0)
        seen[identity] = old | mask
        counts[failure.gap] = counts.get(failure.gap, 0) + int(fresh)
        integer(counts[failure.gap])
        own_days = days.setdefault(failure.gap, {})
        for day in failure.days:
            if not old & day_bits[day]:
                own_days[day] = own_days.get(day, 0) + 1
        prefix = prefixes.setdefault(failure.gap, {})
        if key in prefix:
            before = prefix[key]
            prefix[key] = replace(before,
                object_ids=tuple(sorted(set(before.object_ids) | set(failure.object_ids))),
                days=tuple(sorted(set(before.days) | set(failure.days))))
        elif fresh and (len(prefix) < 16 or key < max(prefix)):
            prefix[key] = failure
            if len(prefix) > 16:
                del prefix[max(prefix)]
    retained = sorted((f for prefix in prefixes.values() for f in prefix.values()),
                      key=lambda f: canonical([f.gap, f.subject]))
    return FailureSet(retained, {name: FailureSummary(count, days[name]) for name, count in counts.items()})


def producer(row: a2.FailureRule) -> Callable[[Context], FailureSet]:
    def decide(ctx: Context) -> FailureSet:
        def assertions() -> Iterable[Failure]:
            for issue in ctx.issues:
                if issue.gap != row.name:
                    continue
                for sub in subjects(ctx, row, issue.subject):
                    a2.GATE_FAILURE(dict(**TAG, gap=row.name, subject=sub, count=1))
                    day = sub['day'] or issue.subject.day
                    days = (day,) if day else ctx.bundle.days
                    yield Failure(row.name, row.rule, sub, 1, issue.object_ids, issue.detail, days)
        return bounded(assertions())
    decide.__name__ = 'decide_' + row.name
    return decide


PRODUCERS = {row.name: producer(row) for row in a2.FAILURE_RULES}


def collect(ctx: Context, required: tuple[str, ...]) -> FailureSet:
    prefix: list[Failure] = []
    summaries: dict[str, FailureSummary] = {}
    for row in a2.FAILURE_RULES:
        if row.rule in required:
            own = PRODUCERS[row.name](ctx)
            prefix.extend(own)
            summaries.update(own.summaries)
    return FailureSet(prefix, summaries)


def listing(failures: Iterable[Failure], days: tuple[str, ...]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    bounded_failures = failures if isinstance(failures, FailureSet) else bounded(failures)
    tallies = {}
    listed = []
    for row in a2.FAILURE_RULES:
        summary = bounded_failures.summaries.get(row.name, FailureSummary(0, {}))
        tallies[row.name] = dict(count=summary.count, listed=min(summary.count, 16), complete=summary.count <= 16,
            day_counts=[summary.day_counts.get(day, 0) for day in days])
    listed = [dict(**TAG, gap=f.gap, subject=f.subject, count=1) for f in bounded_failures]
    listed.sort(key=lambda v: canonical([v['gap'], v['subject']]))
    a2.FAILURE_COUNTS(tallies)
    return tallies, listed


def detail(entries: list[dict[str, Any]], stream: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    limit = roots.STREAMS[stream][2]
    pages: list[dict[str, Any]] = []
    for at in range(0, len(entries), limit):
        pages.append(dict(**TAG, page_seq=f'{len(pages) + 1:020}',
            previous_sha256=sha256(pages[-1]) if pages else None, entries=entries[at:at + limit]))
    ref = dict(root=roots.entry_root(entries, stream=stream), count=len(entries), pages=len(pages),
               tail_sha256=sha256(pages[-1]) if pages else None)
    roots.validate_detail(ref, pages, stream=stream)
    return ref, pages


def normalize(ctx: Context) -> None:
    """Reject unencodable identities at N before any producer is evaluated."""
    rules = {row.name: row for row in a2.FAILURE_RULES}
    damaged = False
    unavailable = False
    for index, issue in enumerate(ctx.issues):
        unavailable |= issue.unavailable
        try:
            for sub in subjects(ctx, rules[issue.gap], issue.subject):
                a2.GATE_FAILURE(dict(**TAG, gap=issue.gap, subject=sub, count=1))
                if sub['scope'] == 'run' and issue.gap not in a2.RUN_SCOPED_FAILURES:
                    unavailable = True
                    ctx.issues[index] = replace(issue, unavailable=True)
        except ValueError:
            damaged = True
            ctx.issues[index] = replace(issue, subject=Subject(wire=a2.N()), unavailable=True)
    if damaged:
        ctx.issue('evidence_schema_gap', 'unencodable required assertion identity')
    if unavailable:
        ctx.issue('artifact_gap', 'assertion population cannot be enumerated')
