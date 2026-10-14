"""Offline source-driven window commands; all input paths come from a plan."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import CHECKS, Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_accounting_stream import (
    compare_export_accounting_streamed,
)
from trusted_router.async_settle_shadow_v2_pack_captured_budget import CapturedBudget
from trusted_router.async_settle_shadow_v2_pack_composition import compose_day
from trusted_router.async_settle_shadow_v2_pack_external import external_pack
from trusted_router.async_settle_shadow_v2_pack_history import history
from trusted_router.async_settle_shadow_v2_pack_originals import DayFiles
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles, input_plan
from trusted_router.async_settle_shadow_v2_pack_procedures import check_scope, procedure_result
from trusted_router.async_settle_shadow_v2_pack_review import review_days
from trusted_router.async_settle_shadow_v2_pack_stage import PRODUCERS, references
from trusted_router.async_settle_shadow_v2_schema import require
from trusted_router.detached_jws import canonical


def collect_day(source: Path, out: Path, plan: dict[str, Any], *,
                reconciliation: Any = None) -> dict[str, Any]:
    """Recompute each package producer from finite captured originals."""
    captures = plan['captures']
    for producer in PRODUCERS:
        require(producer in captures and bool(captures[producer]), 'MissingProducer:' + producer)
        pending = list(references(captures[producer]))
        seen: dict[str, dict[str, Any]] = {}
        while pending:
            ref = pending.pop()
            if ref['path'] in seen:
                require(seen[ref['path']] == ref, 'MissingProducer:' + producer)
                continue
            seen[ref['path']] = ref
            try:
                raw = Files(source).read(ref)
            except (OSError, ValueError) as exc:
                raise ValueError('MissingProducer:' + producer + ':' + ref['path']) from exc
            try:
                value = retained_json(raw)
            except ValueError:
                continue
            children = list(references(value))
            if type(value) is dict and value.get('format') == 'shadow-v2-live-export-a3-n':
                prefix = ref['path'].rsplit('/', 1)[0] + '/' if '/' in ref['path'] else ''
                children = [{**child, 'path': prefix + child['path']} for child in children]
            pending.extend(children)
    files = DayFiles(out)
    files.capture(Files(source), plan['files'])
    native_spec = retained_json(files.read(captures['NativeExport']))
    bundle_path = source / native_spec['bundle']
    require(bundle_path.resolve().is_relative_to(source.resolve()) and bundle_path.is_file(),
            'MissingProducer:NativeExport')
    require(plan['days'] == [plan['day']], 'FiniteDayPlan')
    require(bool(native_spec['passes']) and len({p['id'] for p in native_spec['passes']})
            == len(native_spec['passes']), 'FinitePassPlan')
    require([p['id'] for p in native_spec['passes']] == plan['pass_ids'], 'FinitePassPlan')
    require(len({p['accounting']['path'] for p in native_spec['passes']}) == len(native_spec['passes']),
            'FinitePassPlan')
    require(any(all(p.get(key) == native_spec.get(key) for key in
        ('bundle', 'accounting', 'prefix', 'object_plan')) for p in native_spec['passes']), 'FinitePassPlan')
    comparisons = []
    accounting_roots = []
    accounting_missing = []
    for passed in native_spec['passes']:
        accounting_roots.append(passed['accounting'])
        passed_bundle = (source / passed['bundle']).resolve()
        require(passed_bundle.is_relative_to(source.resolve()) and passed_bundle.is_file(),
                'MissingProducer:NativeExport:' + passed['id'])
        compared = compare_export_accounting_streamed(Files(source), passed['accounting'], passed_bundle,
            day=plan['day'], object_plan=passed['object_plan'], prefix=passed.get('prefix', ''))
        comparisons.append(compared)
        accounting_missing.extend(passed['id'] + ':' + gap for gap in compared.missing)
    # The original export is never edited; collector objects enter the composed graph.
    from trusted_router.async_settle_shadow_v2_pack_records import captured_native
    native = captured_native(bundle_path, out / '.composition-native')
    for item in native_spec.get('objects', []):
        native['attachments'].append(dict(kind=item['kind'], id=item['id'],
            raw=files.read(item['original']).decode()))
    inventory, ready, shutdown = (retained_json(files.read(captures[name]))
                                   for name in ('Inventory', 'Readiness', 'Shutdown'))
    from trusted_router.async_settle_shadow_v2_pack_passes import (
        deployment_accounting,
        measured_export_costs,
    )
    inventory = deployment_accounting(files, inventory, day=plan['day'], roots=accounting_roots,
        review=plan.get('deployment_accounting_review'))
    budget_source = retained_json(files.read(captures['Budget']))
    trial_source = retained_json(files.read(budget_source['trial_record']))
    require(trial_source.get('shutdown_inventory') == captures['Shutdown'], 'BudgetShutdownPopulation')
    require({(p['day'], p['boot']) for p in trial_source['population']} ==
        {(plan['day'], b['boot']) for b in inventory['boots'] if b['role'] == 'router'},
        'BudgetWriterPopulation')
    measured_export_costs(files, budget_source, comparisons, native_spec['output_receipts'])
    budget_source['export_accounting'] = dict(roots=accounting_roots, receipts=native_spec['output_receipts'],
        totals=files.refs['budget/export-compared-totals.json'])
    measured = CapturedBudget.build(files, budget_source, checkpoint=plan['checkpoint'])
    external = retained_json(files.read(captures['ExternalPack']))
    supplied = []
    for procedure in external['procedures']:
        result = procedure_result(files, procedure, capture_root=source)
        name, stage = procedure['name'], procedure['stage']
        supplied.append(dict(name=name, stage=stage, router_revision=result['router_revision'],
            enclave_revision=result['enclave_revision'],
            evidence=files.refs['external/' + stage + '/' + name + '/result.json'],
            approval=external['approvals'][stage + '/' + name]))
    external_pack(files, stage=plan['checkpoint'], required_stages=plan['required_stages'], supplied=supplied)
    history(files, **retained_json(files.read(captures['History'])))
    budget_plan = retained_json(files.read(captures['Budget']))
    from trusted_router.async_settle_shadow_v2_pack_sources import derived_peaks
    segments = [{**s['fields'], 'peaks': derived_peaks(files, s['derivations'])}
                for s in budget_plan['post']['load_segments']]
    producers = dict(NativeExport=native, Inventory=inventory, Readiness=ready, Shutdown=shutdown,
        Budget=dict(pre=files.refs['proof.pre_budget_sha256'], post=files.refs['proof.post_budget_sha256'],
            segments=segments), ExternalPack=files.refs['proof.external_proofs_sha256'],
        History=files.refs['proof.resolution_dag_sha256'])
    require(measured.pre is not None and measured.post is not None, 'MissingProducer:Budget')
    result = compose_day(files, day=plan['day'], producers=producers, fixtures=plan['fixtures'],
        cohort=plan['cohort'], source_shas=plan['source_shas'], complete_through_us=plan['complete_through_us'],
        reconciliation=reconciliation)
    for producer in PRODUCERS:
        files.read(captures[producer])
    files.obj('stage/producer-captures.json', captures)
    criteria: dict[str, list[str]] = {name: list(checks) for name, checks in CHECKS.items()}
    for stage, names in plan['required_stages'].items():
        for name in names:
            criteria[name] = sorted(set(criteria.get(name, [])) | set(check_scope(name, stage)))
    review = review_days(files, days=[plan['day']], subjects={plan['day']: result['subject']},
        criteria=criteria, required_stages=plan['required_stages'], verifiers=plan['verifiers'])
    missing = [*result['missing'], *accounting_missing]
    if not result['manifest']['complete']:
        missing.append('IncompleteDay:' + plan['day'])
    if reconciliation is None:
        missing.extend('MissingSupportDay:' + d for d in result['support_days'] if d != plan['day'])
    # Stream registered originals into the transport without a second byte graph.
    (out / 'stage').mkdir(exist_ok=True)
    missing.extend(write_composed_transport(files, result.pop('transport'), out / 'stage/transport.json'))
    planned = input_plan(files, list(files.refs.values()), days=[plan['day']], predecessors=plan['predecessors'])
    checked = evaluate_stage(out / 'stage/transport.json', out, plan['checkpoint'])
    missing.extend(checked['missing'])
    return dict(complete=not missing, missing=sorted(set(missing)), gate=checked, review=review,
        checkpoint=plan['checkpoint'], inputs=planned, subject=result['subject'],
        transport_sha256=transport_digest(out / 'stage/transport.json'))


ACTION_SEAMS = {'trial': 'P6NativeQualification', 'release': 'P6ReviewedRelease', 'probe': 'P6DedicatedJob'}


def evaluate_stage(bundle: Path, out: Path, stage: str, release_sha256: str | None = None) -> dict[str, Any]:
    """Retain the existing loader, evaluator and renderer's exact result."""
    from scripts.async_settle.shadow_report import shadow_v2_ports
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_release import decode_release_plan
    from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput, render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    release = None
    if release_sha256 is not None:
        release = decode_release_plan((out / 'loading/release-plan.json').read_bytes(), release_sha256)
    ports = shadow_v2_ports(b'[]', b'[]', release=release)
    loaded = load(bundle.read_bytes(), ports)
    result = evaluate(loaded, ports, gate=stage)
    rendered = render(result)
    target = out / 'gate'
    target.mkdir(exist_ok=True)
    value = dict(verdict=result.verdict, gaps=list(result.gaps),
        failures=[dict(gap=f.gap, rule=f.rule, subject=f.subject, count=f.count, days=list(f.days))
                  for f in result.failures], loading=loaded.a2_streams.get('loading_issues', []))
    missing = list(result.gaps)
    if isinstance(rendered, UnavailableGateOutput):
        value['unavailable'] = rendered.diagnostic()
        missing.append('UnavailableGateOutput')
    else:
        (target / 'output.json').write_bytes(rendered[0])
        for index, raw in enumerate(rendered[1]):
            (target / f'{index:05d}.json').write_bytes(raw)
    (target / 'result.json').write_bytes(canonical(value))
    return dict(**value, missing=missing)


def transport_digest(path: Path) -> str:
    with path.open('rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


def write_composed_transport(files: DayFiles, transport: dict[str, Any], target: Path) -> list[str]:
    from trusted_router.async_settle_shadow_v2_export import SpoolArray, spool_transport
    spool = target.parent / (target.stem + '-spool')
    spool.mkdir()
    arrays = {name: SpoolArray(spool / (name + '.jsonl'))
              for name in ('rows', 'attachments', 'ledgers', 'preimages')}
    for name in ('rows', 'attachments', 'ledgers'):
        for value in transport.pop(name, []):
            arrays[name].append(value)
    transport.pop('preimages', None)
    missing = []
    for path, ref in files.refs.items():
        try:
            raw = files.read(ref).decode()
        except UnicodeDecodeError:
            missing.append('UnavailableTransportInput:' + path)
            continue
        arrays['preimages'].append(dict(name=path, raw=raw))
    for path, ref in files.large.items():
        arrays['preimages'].append(dict(name=path, raw=files.original(ref).decode()))
    spool_transport(target, {**transport, **arrays})
    return missing


def write_transport(files: PackFiles, original: Path, target: Path) -> list[str]:
    from trusted_router.async_settle_shadow_v2_export import SpoolArray, spool_transport
    from trusted_router.async_settle_shadow_v2_pack_streaming import CapturedJSON
    spool = target.parent / (target.stem + '-spool')
    spool.mkdir()
    arrays = {name: SpoolArray(spool / (name + '.jsonl'))
              for name in ('rows', 'attachments', 'ledgers', 'preimages')}
    metadata = {}
    with original.open() as source:
        for name, value in CapturedJSON(source).members():
            if name == 'preimages':
                continue
            if name in arrays:
                arrays[name].append(value)
            else:
                metadata[name] = value
    missing = []
    for path, ref in sorted(files.refs.items()):
        try:
            raw = files.read(ref).decode()
        except UnicodeDecodeError:
            missing.append('UnavailableTransportInput:' + path)
            continue
        arrays['preimages'].append(dict(name=path, raw=raw))
    spool_transport(target, {**metadata, **arrays})
    return missing


def window(command: str, plan: dict[str, Any], *, source: Path, out: Path | None,
           stage: str | None = None) -> dict[str, Any]:
    require(command not in ACTION_SEAMS, ACTION_SEAMS.get(command, 'artifact_gap'))
    if command == 'pack' and 'captured_transport' in plan:
        from trusted_router.async_settle_shadow_v2_pack_streaming import split_transport
        require(out is not None, 'artifact_gap')
        assert out is not None
        split = split_transport(source / plan['captured_transport'], out)
        return dict(complete=False, missing=['MultiBundleLoading'], input_objects=split['objects'])
    if 'stages' not in plan:
        from scripts.async_settle.shadow_v2_pack import execute
        require(command in ('review-inputs', 'pack'), 'artifact_gap')
        return execute(command, plan, source, out or source / 'review-inputs')
    captured = None if command == 'collect' else retained_json((source / 'stage/result.json').read_bytes())
    if stage is None:
        stage = captured['stage'] if captured else plan.get('default_stage')
    require(stage in plan['stages'], 'artifact_gap')
    selected = plan['stages'][stage]
    out = out or source / (command + '-output' if command != 'review-inputs' else 'review-inputs')
    require(not out.exists(), 'artifact_gap')
    out.mkdir(parents=True)
    if command == 'collect':
        try:
            if 'day_plans' in selected:
                from trusted_router.async_settle_shadow_v2_pack_window import collect_window
                result = collect_window(source, out, selected, collect_day)
            else:
                result = collect_day(source, out, selected)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result = dict(complete=False, missing=[str(exc)], checkpoint=selected.get('checkpoint'))
        (out / 'stage').mkdir(exist_ok=True)
        result['stage'] = stage
        (out / 'stage/result.json').write_bytes(canonical(result))
        return result
    assert captured is not None
    if 'inputs' not in captured or 'review' not in captured:
        result = dict(complete=False, missing=[*captured['missing'], 'IncompleteCapture'])
        (out / 'result.json').write_bytes(canonical(result))
        return result
    from trusted_router.async_settle_shadow_v2_pack_plan import (
        check_plan,
        review_subjects,
        seal_review,
    )
    refs = check_plan(Files(source), captured['inputs'])
    files = PackFiles(out)
    files.capture(Files(source), [r for r in refs
        if r['path'] not in ('loading/release-plan.json', 'review/subjects.json')])
    # Copy all pages used to locate originals before rebuilding the final plan.
    files.capture(Files(source), captured['inputs']['inventory_pages'])
    files.capture(Files(source), [b['bundle'] for b in captured['inputs']['bundles']])
    files.obj('inputs/plan.json', captured['inputs'])
    missing = list(captured.get('missing', []))
    decisions = selected.get('daily_reviews', {})
    from trusted_router.async_settle_shadow_v2_collect_ports import approval
    for day, subject in captured['review']['subjects'].items():
        if day not in decisions:
            missing.append('ReleaseReview:' + day)
            continue
        files.capture(Files(source), [decisions[day]['record']])
        approval(files, decisions[day], subject)
    previous = retained_json(Files(source).read(captured['review']['release_plan']))
    subjects = review_subjects(files, captured['review']['subjects'], criteria=previous['criteria'],
        required_stages=previous['required_stages'], verifiers=previous['verifiers'],
        window=captured.get('kind') == 'window')
    if command == 'review-inputs':
        return dict(complete=False, missing=sorted(set([*missing, 'ReleaseReview'])), review=subjects)
    require(command == 'pack', 'artifact_gap')
    digest = selected.get('release_plan_sha256')
    if digest is None or set(decisions) != set(subjects['subjects']):
        missing.append('ReleaseReview')
    else:
        seal_review(files, subjects, approvals=decisions, release_sha256=digest)
    # Exclude earlier input pages to avoid repeatedly nesting old plans.
    planned = input_plan(files, [r for p, r in files.refs.items() if not p.startswith('inputs/')],
        days=captured['inputs']['days'], predecessors=captured['inputs']['predecessors'], prefix='final-inputs')
    check_plan(Files(out), planned)
    if captured.get('kind') == 'window':
        result = dict(complete=False, kind='window', missing=sorted(set([*missing, 'MultiBundleLoading'])),
            inputs=planned, window=captured['window'], review=subjects)
        files.obj('result.json', result)
        return result
    original = source / 'stage/transport.json'
    require(transport_digest(original) == captured['transport_sha256'], 'artifact_gap')
    missing.extend(write_transport(files, original, out / 'bundle.json'))
    checked = evaluate_stage(out / 'bundle.json', out, selected['checkpoint'], digest)
    missing.extend(checked['missing'])
    result = dict(complete=not missing, missing=sorted(set(missing)), gate=checked,
        bundle='bundle.json', inputs=planned, review=subjects)
    files.obj('result.json', result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('collect', 'review-inputs', 'pack', *ACTION_SEAMS))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--stage')
    parser.add_argument('--phase')
    args = parser.parse_args(argv)
    try:
        result = window(args.command, retained_json(args.plan.read_bytes()), source=args.input or args.plan.parent,
                        out=args.out, stage=args.stage)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        result = dict(complete=False, missing=[str(exc)])
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
