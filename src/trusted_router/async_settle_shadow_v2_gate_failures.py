"""A2 assertion identities and deterministic listings, using the contract table."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, replace
from itertools import groupby
from typing import TYPE_CHECKING, Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router import async_settle_shadow_v2_a2_roots as roots
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_result import Failure, Subject
from trusted_router.async_settle_shadow_v2_schema import day_at, integer, sha256
from trusted_router.detached_jws import canonical

if TYPE_CHECKING:
    from trusted_router.async_settle_shadow_v2_gate_bundle import Context, Issue

TAG = dict(v=2, policy_version='shadow-v2', contract_revision='a2')


def writer_subjects(ctx: Context, sub: Subject) -> list[dict[str, Any]]:
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
    return [a2.W(d, r, b) for d, r, b in sorted(found)
            if (sub.day is None or sub.day == d) and (sub.boot is None or sub.boot == b)]


def subjects(ctx: Context, row: a2.FailureRule, sub: Subject) -> list[dict[str, Any]]:
    if sub.wire is not None:
        return [sub.wire]
    codes = row.constructor
    if codes == ('N',):
        return [a2.N()]
    if codes == ('D',):
        return [a2.D(sub.day)] if sub.day else [a2.D(d) for d in ctx.bundle.days] or [a2.N()]
    if codes == ('V',):
        return [a2.V(sub.object_id)] if sub.object_id else [a2.N()]
    if codes == ('A',):
        return [a2.A('pre')]
    if codes == ('K',):
        stages = ('pre',) if ctx.gate == 'fleet-budgets-pre' else ('pre', 'post')
        return [a2.K(stage, sub.object_id) for stage in stages]
    if codes == ('P',):
        name = 'pilot' if row.name == 'pilot_prerequisite_gap' else sub.object_id
        return [a2.P(ctx.gate, name)] if name else [a2.N()]
    if codes == ('Q',):
        return [a2.N()]  # Missing reset identity never fabricates a correctness fact.
    if 'B' in codes:
        if sub.day and sub.boot and sub.phase in ('settle', 'refund') and sub.dimensions:
            return [a2.B(sub.day, sub.boot, sub.phase, asdict(sub.dimensions))]
        return [a2.N()]
    if 'W' in codes:
        values = writer_subjects(ctx, sub)
        if row.rule != 'G2':
            values = [v for v in values if v['role'] == 'router']
        return values or [a2.N()]
    if codes == ('E',):
        found = [a2.E(p['day'], p['boot'], p['first_seq'], ev['seq'], event=ev)
                 for p, ev in ctx.events() if (sub.day is None or p['day'] == sub.day)
                 and (sub.boot is None or p['boot'] == sub.boot) and ev['seq'] == sub.object_id]
        return found or [a2.N()]
    if row.name in ('close_result_identity_gap', 'close_result_conflict'):
        key = sub.day + '/' + sub.boot if sub.day and sub.boot else sub.object_id
        return [a2.O('async_settle_shadow_close_result_v2', key)] if key else [a2.N()]
    if sub.object_id:
        candidates = [r.key for r in (*ctx.bundle.rows, *ctx.bundle.attachments) if r.key.id == sub.object_id]
        if len(set(candidates)) == 1:
            return [a2.O(candidates[0].kind, candidates[0].id)]
        return [a2.O('a2_file', sub.object_id)]
    return [a2.N()]


def producer(row: a2.FailureRule) -> Callable[[Context], tuple[Failure, ...]]:
    def decide(ctx: Context) -> tuple[Failure, ...]:
        assertions: list[tuple[bytes, dict[str, Any], Issue]] = []
        for issue in ctx.issues:
            if issue.gap != row.name:
                continue
            for sub in subjects(ctx, row, issue.subject):
                value = dict(**TAG, gap=row.name, subject=sub, count=1)
                a2.GATE_FAILURE(value)
                assertions.append((canonical(sub), sub, issue))
        assertions.sort(key=lambda v: v[0])
        result = []
        for _, items in groupby(assertions, key=lambda v: v[0]):
            group = list(items)
            wire = group[0][1]
            # Unknown affected time blocks all scoped days; diagnosed day remains
            # attribution, never optional enrichment of the canonical subject.
            days: set[str] = set()
            for _, _, issue in group:
                days.update((issue.subject.day,) if issue.subject.day else ctx.bundle.days)
            result.append(Failure(row.name, row.rule, wire, 1,
                tuple(sorted({key for _, _, issue in group for key in issue.object_ids})),
                '; '.join(sorted({issue.detail for _, _, issue in group})), tuple(sorted(days))))
        return tuple(result)
    decide.__name__ = 'decide_' + row.name
    return decide


# This is a derived dispatch registry, not a second inventory of names/units.
PRODUCERS = {row.name: producer(row) for row in a2.FAILURE_RULES}


def listing(failures: Iterable[Failure], days: tuple[str, ...]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    tallies = {}
    listed: list[dict[str, Any]] = []
    materialized = sorted(failures, key=lambda f: canonical([f.gap, f.subject]))
    for row in a2.FAILURE_RULES:
        own = [f for f in materialized if f.gap == row.name]
        integer(len(own))
        tallies[row.name] = dict(count=len(own), listed=min(len(own), 16), complete=len(own) <= 16,
            day_counts=[sum(day in f.days for f in own) for day in days])
        listed.extend(dict(**TAG, gap=f.gap, subject=f.subject, count=1) for f in own[:16])
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
    for index, issue in enumerate(ctx.issues):
        try:
            for sub in subjects(ctx, rules[issue.gap], issue.subject):
                a2.GATE_FAILURE(dict(**TAG, gap=issue.gap, subject=sub, count=1))
        except ValueError:
            damaged = True
            ctx.issues[index] = replace(issue, subject=Subject(wire=a2.N()))
    if damaged:
        ctx.issue('evidence_schema_gap', 'unencodable required assertion identity')
