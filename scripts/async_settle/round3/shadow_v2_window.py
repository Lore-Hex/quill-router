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


def collect_day(source: Path, out: Path, plan: dict[str, Any]) -> dict[str, Any]:
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
    compared = compare_export_accounting_streamed(Files(source), native_spec['accounting'], bundle_path,
        day=plan['day'], object_plan=native_spec['object_plan'], prefix=native_spec.get('prefix', ''))
    require(compared.complete, 'MissingProducer:NativeExport:' + ','.join(compared.missing))
    # The original export is never edited; collector objects enter the composed graph.
    native = retained_json(bundle_path.read_bytes(), limit=2621440000)
    native['attachments'] = [*native.get('attachments', []), *[
        dict(kind=item['kind'], id=item['id'], raw=files.read(item['original']).decode())
        for item in native_spec.get('objects', [])]]
    inventory, ready, shutdown = (retained_json(files.read(captures[name]))
                                   for name in ('Inventory', 'Readiness', 'Shutdown'))
    measured = CapturedBudget.build(files, retained_json(files.read(captures['Budget'])), checkpoint=plan['checkpoint'])
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
        cohort=plan['cohort'], source_shas=plan['source_shas'], complete_through_us=plan['complete_through_us'])
    for producer in PRODUCERS:
        files.read(captures[producer])
    files.obj('stage/producer-captures.json', captures)
    criteria: dict[str, list[str]] = {name: list(checks) for name, checks in CHECKS.items()}
    for stage, names in plan['required_stages'].items():
        for name in names:
            criteria[name] = sorted(set(criteria.get(name, [])) | set(check_scope(name, stage)))
    review = review_days(files, days=[plan['day']], subjects={plan['day']: result['subject']},
        criteria=criteria, required_stages=plan['required_stages'], verifiers=plan['verifiers'])
    originals = []
    for path, ref in files.refs.items():
        raw = files.read(ref)
        try:
            text = raw.decode()
        except UnicodeDecodeError:
            continue
        originals.append(dict(name=path, raw=text))
    originals.extend(dict(name=path, raw=files.original(ref).decode()) for path, ref in files.large.items())
    result['transport']['preimages'] = originals
    # Bundle assembly is kept separate from release review; no approval is made.
    (out / 'stage').mkdir(exist_ok=True)
    (out / 'stage/transport.json').write_bytes(canonical(result['transport']))
    planned = input_plan(files, list(files.refs.values()), days=[plan['day']], predecessors=plan['predecessors'])
    return dict(complete=True, review=review, inputs=planned, subject=result['subject'],
        transport_sha256=hashlib.sha256((out / 'stage/transport.json').read_bytes()).hexdigest())


def window(command: str, plan: dict[str, Any], *, source: Path, out: Path, stage: str) -> dict[str, Any]:
    require(stage in plan['stages'], 'artifact_gap')
    selected = plan['stages'][stage]
    require(not out.exists(), 'artifact_gap')
    if command == 'collect':
        out.mkdir(parents=True)
        result = collect_day(source, out, selected)
        (out / 'stage/result.json').write_bytes(canonical(result))
        return result
    captured = retained_json((source / 'stage/result.json').read_bytes())
    from trusted_router.async_settle_shadow_v2_pack_plan import check_plan
    refs = check_plan(Files(source), captured['inputs'])
    require(captured['complete'] is True, 'artifact_gap')
    out.mkdir(parents=True)
    files = PackFiles(out)
    files.capture(Files(source), refs)
    if command == 'review-inputs':
        files.obj('review/subjects.json', captured['review'])
        return dict(complete=False, missing=['ReleaseReview'], review=captured['review'])
    require(command == 'pack', 'artifact_gap')
    # Approval records are supplied after subjects have been prepared.
    from trusted_router.async_settle_shadow_v2_collect_ports import approval
    decisions = selected.get('daily_reviews', {})
    for day, ref in captured['review']['subjects'].items():
        require(day in decisions, 'ReleaseReview')
        record = decisions[day]['record']
        files.capture(Files(source), [record])
        approval(files, decisions[day], ref)
    raw = (source / 'stage/transport.json').read_bytes()
    require(hashlib.sha256(raw).hexdigest() == captured['transport_sha256'], 'artifact_gap')
    (out / 'bundle.json').write_bytes(raw)
    return dict(complete=True, bundle='bundle.json', inputs=captured['inputs'])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('collect', 'review-inputs', 'pack'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--stage', required=True)
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
