"""Explicit offline pack commands over finite captured files; no native clients."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import Files
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_plan import (
    PackFiles,
    check_plan,
    input_plan,
    review_subjects,
    seal_review,
)


def execute(command: str, plan: dict[str, Any], source: Path, out: Path, *,
            release_sha256: str | None = None) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=False)
    files = PackFiles(out)
    captured = Files(source)
    files.capture(captured, plan['files'])
    if command == 'review-inputs':
        result = review_subjects(files, plan['subjects'], criteria=plan['criteria'],
            required_stages=plan['required_stages'], verifiers=plan['verifiers'])
        return dict(complete=False, missing=['ReleaseReview'], review=result)
    if command == 'seal':
        if release_sha256 is None:
            raise ValueError('independent_release_digest_required')
        return dict(complete=True, review=seal_review(files, plan['review'],
                    approvals=plan['approvals'], release_sha256=release_sha256))
    if command == 'plan':
        result = input_plan(files, list(files.refs.values()), days=plan['days'],
                            predecessors=plan['predecessors'])
        check_plan(files, result)
        return dict(complete=True, plan=result)
    return pack_captured(files, plan, release_sha256=release_sha256)


def pack_captured(files: PackFiles, plan: dict[str, Any], *,
                  release_sha256: str | None = None) -> dict[str, Any]:
    """Assemble a reviewed captured graph and leave actual stage checks to P5."""
    from trusted_router import async_settle_shadow_v2_a1_evidence as a1
    from trusted_router import async_settle_shadow_v2_a2_contract as contract
    from trusted_router.async_settle_shadow_v2_pack_daypack import (
        assemble_day,
        compare_export_accounting,
    )
    from trusted_router.detached_jws import canonical

    assembly = plan.get('assembly')
    if assembly is None:
        return dict(complete=False, missing=['StageAssemblyInputs'])
    passes = plan.get('accounting_passes', [])
    if not passes:
        return dict(complete=False, missing=['ExportAccountingInputs'])
    roots_seen = set()
    accounting_missing: list[str] = []
    for item in passes:
        identity = item['root']['path']
        if identity in roots_seen:
            raise ValueError('duplicate_accounting_pass')
        roots_seen.add(identity)
        compared = compare_export_accounting(files, item['root'], files.read(item['bundle']),
            day=item['day'], object_plan=item['object_plan'], prefix=item.get('prefix', ''),
            require_native=not plan.get('offline_test', False))
        accounting_missing.extend(identity + ':' + reason for reason in compared.missing)
    if set(plan['days']) != {item['day'] for item in passes}:
        accounting_missing.append('planned_day_accounting_missing')
    if accounting_missing:
        return dict(complete=False, missing=accounting_missing)
    artifacts = {name: files.read(ref) for name, ref in assembly['artifacts'].items()}
    root_values = dict(assembly['roots'])
    for qualified in contract.ENTRY_PREIMAGES:
        root_values[qualified] = [dict(key=item['key'], bodies=[files.read(ref) for ref in item['bodies']])
                                  for item in root_values[qualified]]
    expected = {}
    for ref in assembly['expected_sources']:
        files.read(ref)
        expected[ref['path']] = True
    manifest, proof = assemble_day(files, assembly['manifest'], assembly['proof'], artifacts=artifacts,
        preimages=root_values, expected_closed=expected, horizon_ends=assembly['horizon_ends'],
        complete_through_us=assembly['complete_through_us'])
    transport = dict(plan['transport'])
    transport['proof'] = a1.PROOF_MANIFEST.serialize(proof).decode()
    rows = list(transport.get('rows', []))
    rows.append(dict(kind=a1.KINDS[5], id=manifest['day'] + '/manifest-v2',
                     raw=a1.MANIFEST.serialize(manifest).decode()))
    transport['rows'] = rows
    transport['preimages'] = [dict(name=path, raw=raw.decode()) for path, raw in files.originals().items()]
    raw = canonical(transport)
    with (files.root / 'bundle.json').open('xb') as output:
        output.write(raw)
    if release_sha256 is None:
        return dict(complete=False, missing=['ReleaseReview'], bundle='bundle.json')
    return evaluate_captured(files, raw, stage=plan['stage'], release_sha256=release_sha256)


def evaluate_captured(files: PackFiles, raw: bytes, *, stage: str, release_sha256: str) -> dict[str, Any]:
    from scripts.async_settle.async_settle_shadow_v2_gate_tools import read_release_plan
    from scripts.async_settle.shadow_report import shadow_v2_ports
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput, render
    from trusted_router.async_settle_shadow_v2_gates import evaluate

    release = read_release_plan(files.read(files.refs['loading/release-plan.json']), release_sha256)
    ports = shadow_v2_ports(files.read(files.refs['loading/certificate-public.json']),
                           files.read(files.refs['loading/binding-public.json']), release=release)
    result = evaluate(load(raw, ports), ports, gate=stage)
    rendered = render(result)
    if isinstance(rendered, UnavailableGateOutput):
        return dict(complete=False, missing=[rendered.diagnostic()], bundle='bundle.json')
    output, attachments = rendered
    files.put('gate/report.json', attachments[0], a2.GATE_REPORT)
    files.put('gate/output.json', output, a2.GATE_OUTPUT)
    for stream, pages in result.detail_streams.items():
        for page in pages:
            files.obj(a2.L('report', stream, page['page_seq']), page)
    for path, original in result.input_leaves.items():
        files.put(path, original)
    return dict(complete=result.verdict == 'PASS', missing=list(result.gaps), bundle='bundle.json',
                output='gate/output.json')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'review-inputs', 'seal', 'pack'))
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--release-plan-sha256')
    args = parser.parse_args(argv)
    existed = args.out.exists()
    try:
        result = execute(args.command, retained_json(args.plan.read_bytes()), args.input, args.out,
                         release_sha256=args.release_plan_sha256)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        result = dict(complete=False, missing=[type(exc).__name__ + ':' + str(exc)])
    if not existed and args.out.is_dir() and not (args.out / 'status.json').exists():
        # An existing pack is never updated, including its earlier failure record.
        with (args.out / 'status.json').open('x') as target:
            json.dump(result, target, sort_keys=True, separators=(',', ':'))
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 0 if result['complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
