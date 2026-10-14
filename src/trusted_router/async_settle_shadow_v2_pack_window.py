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
from trusted_router.async_settle_shadow_v2_pack_stage import PRODUCERS, references
from trusted_router.async_settle_shadow_v2_pack_streaming import CapturedJSON, split_transport
from trusted_router.async_settle_shadow_v2_schema import day as parse_day
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical


def local_root(source: Path, path: str) -> Path:
    root = (source / path).resolve()
    require(root.is_relative_to(source.resolve()) and root.is_dir(), 'MissingDaySource:' + path)
    return root


def reconciliation(source: Path, plans: dict[str, Any], *, selected: list[str] | None = None) -> TerminalReconciliation:
    """Read the finite original day exports before choosing the support dates."""
    from tempfile import TemporaryDirectory

    from trusted_router.async_settle_shadow_v2_pack_records import RecordSpool
    scratch = TemporaryDirectory(prefix='shadow-window-')
    pages = RecordSpool(Path(scratch.name) / 'pages')
    samples = RecordSpool(Path(scratch.name) / 'samples')
    sources = {}
    for day, spec in sorted(plans.items()):
        parse_day(day)
        require(spec['day'] == day and spec['days'] == [day], 'FiniteDayPlan')
        root = local_root(source, spec.get('source_root', '.'))
        capture = retained_json(Files(root).read(spec['captures']['NativeExport']))
        path = (root / capture['bundle']).resolve()
        require(path.is_relative_to(root) and path.is_file(), 'MissingDaySource:' + day)
        sources[day] = Files(root), spec, capture, path
        with path.open() as stream:
            for group, row in CapturedJSON(stream).members():
                if group != 'rows' or row['kind'] not in (p1.KINDS[0], p1.KINDS[2]):
                    continue
                require(row['id'].startswith(day + '/'), 'DaySourcePopulation')
                (samples if row['kind'] == p1.KINDS[0] else pages).append(retained_json(row['raw'].encode()))
    joined = TerminalReconciliation(pages, samples, spool=Path(scratch.name) / 'joined')
    joined.scratch = scratch
    required = joined.required_through(list(plans) if selected is None else selected)
    joined.day_status = {day: support_complete(*args, minimum_through_us=required.get(day, 0))
                         for day, args in sources.items()}
    return joined


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
        namespace = value.get('source_root', '.')
        graph_source = Files(local_root(source.root, namespace))
        originals = check_plan(graph_source, value['inputs'])
        release = decode_release_plan(graph_source.read(value['release_plan']), value['release_sha256'])
        registered = {r['path']: r for r in originals}
        require(value['gate'] in originals, 'PredecessorGate')
        require(set(registered) - {value['gate']['path']} <= set(release.artifacts), 'PredecessorReview')
        for path, digest in release.artifacts.items():
            require(path in registered and registered[path]['sha256'] == digest, 'PredecessorReview')
        for child in value['predecessors']:
            visit(child)
        files.capture(source, [ref])
        for original in [value['release_plan'], *originals, *references(value['inputs'])]:
            path = original['path'] if namespace == '.' else namespace.rstrip('/') + '/' + original['path']
            files.put(path, graph_source.read(original))
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
    interval = plan.get('interval')
    if interval is not None:
        from trusted_router.async_settle_shadow_v2_schema import day_at
        seed = p1.SAMPLE.parse(Files(source).read(interval['seed']))
        require(interval['start_us'] == seed['observed_at_us'] and
                interval['end_us'] - interval['start_us'] >= 7 * 86400000000, 'WindowElapsedCoverage')
        expected = [day_at(day * 86400000000) for day in range(interval['start_us'] // 86400000000,
            (interval['end_us'] - 1) // 86400000000 + 1)]
        require(selected == expected, 'WindowSelectedDays')
    require(set(selected) <= set(plan['day_plans']), 'MissingSelectedDay')
    # Check all declared producer roots before starting expensive daily work.
    # The callback-only compatibility tests have only a NativeExport root.
    for spec in plan['day_plans'].values():
        if 'files' not in spec:
            continue
        capture_files = Files(local_root(source, spec.get('source_root', '.')))
        for producer in PRODUCERS:
            require(bool(spec['captures'].get(producer)), 'MissingProducer:' + producer)
            try:
                capture_files.read(spec['captures'][producer])
            except (OSError, ValueError) as exc:
                raise ValueError('MissingProducer:' + producer) from exc
    joined = reconciliation(source, plan['day_plans'], selected=selected)
    days = joined.support_days(selected)
    files = PackFiles(out)
    previous = predecessors(Files(source), files, plan['predecessors'])
    if interval is not None:
        files.capture(Files(source), [interval['seed']])
    missing = ['MissingSupportDay:' + day for day in days if day not in plan['day_plans']]
    if missing:
        diagnostic = files.obj('window/missing-days.json', dict(days=selected, support_days=list(days), missing=missing))
        planned = input_plan(files, [*files.refs.values()], days=list(days), predecessors=previous)
        return dict(complete=False, kind='window', missing=missing, inputs=planned,
            window=diagnostic, checkpoint=plan['checkpoint'])
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
            path = 'day-inputs/' + day + '/' + ref['path']
            files.refs[path] = {**ref, 'path': path}
        for ref in [*transport_refs, *references(split['inputs'])]:
            path = 'day-inputs/' + day + '/transport-parts/' + ref['path']
            files.refs[path] = {**ref, 'path': path}
        child = dict(day=day, complete=result['complete'], complete_through_us=spec['complete_through_us'],
            root='day-inputs/' + day, inputs=result['inputs'], transport=split,
            subject=result['subject'], gate=result['gate'])
        children[day] = files.obj('window/days/' + day + '.json', child)
        subjects[day] = children[day]
        del result, split, registered, transport_refs
    stage = files.obj('window/stage.json', dict(days=selected, support_days=list(days), children=children,
        predecessors=previous, checkpoint=plan['checkpoint'], interval=interval))
    from trusted_router.async_settle_shadow_v2_collect_ports import CHECKS
    from trusted_router.async_settle_shadow_v2_pack_procedures import check_scope
    from trusted_router.async_settle_shadow_v2_pack_review import review_days
    criteria = {name: list(checks) for name, checks in CHECKS.items()}
    for name, names in plan['required_stages'].items():
        for criterion in names:
            criteria[criterion] = sorted(set(criteria.get(criterion, [])) | set(check_scope(criterion, name)))
    review = review_days(files, days=sorted(subjects), subjects=subjects, criteria=criteria,
        required_stages=plan['required_stages'], verifiers=plan['verifiers'], window=True)
    planned = input_plan(files, list(files.refs.values()), days=list(days), predecessors=previous)
    check_plan(files, planned)
    missing.append('MultiBundleLoading')
    return dict(complete=False, kind='window', missing=sorted(set(missing)), inputs=planned,
        window=stage, review=review, checkpoint=plan['checkpoint'])


def support_complete(files: Files, spec: dict[str, Any], capture: dict[str, Any], path: Path,
                     *, minimum_through_us: int = 0) -> bool:
    """Compare each support day's source roster, receipt tails and horizon cutoff."""
    from tempfile import TemporaryDirectory

    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router.async_settle_shadow_v2_pack_coverage import ExactPages, receipt_chain
    from trusted_router.async_settle_shadow_v2_pack_records import RecordSpool
    if 'files' not in spec:
        return False
    inventory, readiness, shutdown = (retained_json(files.read(spec['captures'][name]))
        for name in ('Inventory', 'Readiness', 'Shutdown'))
    if not all(result.get('complete') is True for result in (inventory, readiness, shutdown)):
        return False
    expected_ledgers = {(b['role'], b['boot'], b['revision'], b['region'], b['boot_at_us'])
                        for b in inventory['boots']}
    ledgers = [a1.PREREQUISITE_LEDGER.parse(files.read(ref)) for ref in readiness['ledgers']]
    actual_ledgers = {(b['role'], b['boot'], b['revision'], b['region'], b['boot_at_us']) for b in ledgers}
    if actual_ledgers != expected_ledgers or len(ledgers) != len(expected_ledgers):
        return False
    for ref in readiness['proofs']:
        files.read(ref)
    expected_shutdown = {(spec['day'], b['boot']) for b in inventory['boots'] if b['role'] == 'router'}
    entries = shutdown['entries']
    if {(entry['day'], entry['boot']) for entry in entries} != expected_shutdown or len(entries) != len(expected_shutdown):
        return False
    for entry in entries:
        for key in ('measurement', 'original_close', 'close_result'):
            files.read(entry[key])

    with TemporaryDirectory(prefix='support-day-') as directory:
        records = RecordSpool(Path(directory) / 'records')
        completed = 0
        with path.open() as stream:
            for group, row in CapturedJSON(stream).members():
                if group == 'completed_at_us':
                    completed = row
                if group == 'rows':
                    records.append(row['raw'], keys=(row['kind'] + '/' + row['id'],))
        if completed < max(spec['complete_through_us'], minimum_through_us):
            return False
        source = ExactPages(lambda key: tuple(raw.encode() for raw in records.lookup(p1.KINDS[2] + '/' + key)))
        for boot in inventory['boots']:
            if boot['role'] != 'router':
                continue
            key = spec['day'] + '/' + boot['boot']
            counters = list(records.lookup(p1.KINDS[3] + '/' + key))
            closures = list(records.lookup(p1.KINDS[4] + '/' + key))
            if len(counters) != 1 or len(closures) != 1:
                return False
            counter = a1.COUNTER.parse(counters[0].encode())
            if not counter['closed'] or receipt_chain(source, counter).tail is not None:
                return False
        return True
