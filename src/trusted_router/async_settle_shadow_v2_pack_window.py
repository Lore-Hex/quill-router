"""Finite day collection with original support days and reviewed predecessors."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_gate_release import decode_release_plan
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, check_plan, input_plan
from trusted_router.async_settle_shadow_v2_pack_references import TerminalReconciliation
from trusted_router.async_settle_shadow_v2_pack_stage import references
from trusted_router.async_settle_shadow_v2_pack_streaming import CapturedJSON, split_transport
from trusted_router.async_settle_shadow_v2_schema import day as parse_day
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical


def local_root(source: Path, path: str) -> Path:
    root = (source / path).resolve()
    require(root.is_relative_to(source.resolve()) and root.is_dir(), 'MissingDaySource:' + path)
    return root


def reconciliation(source: Path, plans: dict[str, Any]) -> TerminalReconciliation:
    """Read the finite original day exports before choosing the support dates."""
    pages: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    for day, spec in sorted(plans.items()):
        parse_day(day)
        require(spec['day'] == day and spec['days'] == [day], 'FiniteDayPlan')
        root = local_root(source, spec.get('source_root', '.'))
        capture = retained_json(Files(root).read(spec['captures']['NativeExport']))
        path = (root / capture['bundle']).resolve()
        require(path.is_relative_to(root) and path.is_file(), 'MissingDaySource:' + day)
        with path.open() as stream:
            for group, row in CapturedJSON(stream).members():
                if group != 'rows' or row['kind'] not in (p1.KINDS[0], p1.KINDS[2]):
                    continue
                require(row['id'].startswith(day + '/'), 'DaySourcePopulation')
                (samples if row['kind'] == p1.KINDS[0] else pages).append(retained_json(row['raw'].encode()))
    return TerminalReconciliation(pages, samples)


def predecessors(source: Files, files: PackFiles, roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy every reviewed predecessor plan and all of its registered originals.

    Each descriptor names an input plan, its independent release digest, its
    original gate output and earlier descriptors. It does not claim a new gate
    result; the existing stage loader must re-evaluate the retained graphs.
    """
    found: dict[str, dict[str, Any]] = {}
    visiting: set[str] = set()

    def visit(ref: dict[str, Any]) -> None:
        require(ref['path'] not in visiting, 'PredecessorCycle')
        if ref['path'] in found:
            require(found[ref['path']] == ref, 'PredecessorConflict')
            return
        visiting.add(ref['path'])
        value = retained_json(source.read(ref))
        require(value['format'] == 'shadow-v2-reviewed-predecessor-1', 'PredecessorPlan')
        originals = check_plan(source, value['inputs'])
        release = decode_release_plan(source.read(value['release_plan']), value['release_sha256'])
        registered = {r['path']: r for r in originals}
        require(value['gate'] in originals, 'PredecessorGate')
        require(set(registered) - {value['gate']['path']} <= set(release.artifacts), 'PredecessorReview')
        for path, digest in release.artifacts.items():
            require(path in registered and registered[path]['sha256'] == digest, 'PredecessorReview')
        for child in value['predecessors']:
            visit(child)
        for original in [ref, value['release_plan'], *originals, *references(value['inputs'])]:
            files.capture(source, [original])
        visiting.remove(ref['path'])
        found[ref['path']] = ref

    for ref in roots:
        visit(ref)
    return [found[key] for key in sorted(found)]


def collect_window(source: Path, out: Path, plan: dict[str, Any],
                   collect: Callable[..., dict[str, Any]]) -> dict[str, Any]:
    """Release one day's call and files before assembling the following day."""
    selected = plan['days']
    require(bool(selected) and selected == sorted(set(selected)), 'FiniteDayPlan')
    for day in selected:
        parse_day(day)
    require(set(selected) <= set(plan['day_plans']), 'MissingSelectedDay')
    joined = reconciliation(source, plan['day_plans'])
    days = joined.support_days(selected)
    files = PackFiles(out)
    previous = predecessors(Files(source), files, plan['predecessors'])
    missing = ['MissingSupportDay:' + day for day in days if day not in plan['day_plans']]
    children, subjects = {}, {}
    for day in days:
        if day not in plan['day_plans']:
            continue
        spec = plan['day_plans'][day]
        root = local_root(source, spec.get('source_root', '.'))
        directory = out / 'day-inputs' / day
        result = collect(root, directory, spec, reconciliation=joined)
        (directory / 'stage/result.json').write_bytes(canonical(result))
        missing.extend(day + ':' + reason for reason in result['missing'])
        split = split_transport(directory / 'stage/transport.json', directory / 'transport-parts')
        registered = check_plan(Files(directory), result['inputs'])
        transport_refs = check_plan(Files(directory / 'transport-parts'), split['inputs'])
        for ref in [*registered, *references(result['inputs'])]:
            files.put('day-inputs/' + day + '/' + ref['path'], Files(directory).read(ref))
        for ref in [*transport_refs, *references(split['inputs'])]:
            files.put('day-inputs/' + day + '/transport-parts/' + ref['path'],
                      Files(directory / 'transport-parts').read(ref))
        child = dict(day=day, complete=result['complete'], complete_through_us=spec['complete_through_us'],
            root='day-inputs/' + day, inputs=result['inputs'], transport=split,
            subject=result['subject'], gate=result['gate'])
        children[day] = files.obj('window/days/' + day + '.json', child)
        subjects[day] = children[day]
        del result, split, registered, transport_refs
    stage = files.obj('window/stage.json', dict(days=selected, support_days=list(days), children=children,
        predecessors=previous, checkpoint=plan['checkpoint']))
    from trusted_router.async_settle_shadow_v2_collect_ports import CHECKS
    from trusted_router.async_settle_shadow_v2_pack_procedures import check_scope
    from trusted_router.async_settle_shadow_v2_pack_review import review_days
    criteria = {name: list(checks) for name, checks in CHECKS.items()}
    for name, names in plan['required_stages'].items():
        for criterion in names:
            criteria[criterion] = sorted(set(criteria.get(criterion, [])) | set(check_scope(criterion, name)))
    review = review_days(files, days=sorted(subjects), subjects=subjects, criteria=criteria,
        required_stages=plan['required_stages'], verifiers=plan['verifiers'])
    planned = input_plan(files, list(files.refs.values()), days=list(days), predecessors=previous)
    check_plan(files, planned)
    missing.append('MultiBundleLoading')
    return dict(complete=False, kind='window', missing=sorted(set(missing)), inputs=planned,
        window=stage, review=review, checkpoint=plan['checkpoint'])
