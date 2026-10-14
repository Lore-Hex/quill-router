"""Finite pass accounting bound to the reviewed deployment and budget."""
from __future__ import annotations

from typing import Any

from trusted_router import async_settle_shadow_v2_a2_evidence as a2
from trusted_router.async_settle_shadow_v2_collect_ports import approval
from trusted_router.async_settle_shadow_v2_gate_loading import retained_json
from trusted_router.async_settle_shadow_v2_pack_budget import export_costs
from trusted_router.async_settle_shadow_v2_pack_daypack import bind_accounting
from trusted_router.async_settle_shadow_v2_pack_plan import PackFiles
from trusted_router.async_settle_shadow_v2_schema import require


def deployment_accounting(files: PackFiles, inventory: dict[str, Any], *, day: str,
                          roots: list[dict[str, Any]], review: dict[str, Any] | None) -> dict[str, Any]:
    deployment = retained_json(files.read(inventory['deployment']))
    # Always derive from the unbound C1 source; a supplied bound record is compared below.
    source = bind_accounting(files, deployment, day=day, accounting=roots)
    ref = files.obj(day + '/deployment-with-accounting.json', source)
    if review is None:
        raise ValueError('DeploymentAccountingReview:' + ref['sha256'])
    checked = approval(files, review, ref)
    envelope = a2.INDEPENDENT_INVENTORY.parse(files.read(inventory['envelope']))
    envelope.update(deployment=ref, approval=checked)
    result = {**inventory, 'deployment': ref,
        'envelope': files.obj(day + '/inventory-with-accounting.json', envelope, a2.INDEPENDENT_INVENTORY)}
    return result


def measured_export_costs(files: PackFiles, plan: dict[str, Any], compared: list[Any],
                          receipts: list[dict[str, Any]]) -> dict[str, int]:
    require('output_plan' in plan, 'MissingOutputPlan')
    planned = retained_json(files.read(plan['output_plan']))
    approval(files, plan['output_plan_review'], plan['output_plan'])
    passes = planned['passes']
    require(len(passes) == len(compared) and len({p['pass'] for p in passes}) == len(passes), 'OutputPassPopulation')
    expected = {}
    for passed, comparison in zip(passes, compared, strict=True):
        require(passed['day'] == comparison.day, 'OutputPassDay')
        require(passed['start_us'] < passed['end_us'], 'OutputPassInterval')
        from trusted_router.async_settle_shadow_v2_schema import day_at
        require(day_at(passed['start_us']) == passed['day'] == day_at(passed['end_us'] - 1), 'OutputPassDay')
        for operation in passed['outputs']:
            key = passed['pass'], operation['operation'], operation['object']['path']
            require(key not in expected, 'OutputPopulation')
            expected[key] = passed, operation
    originals = []
    seen = set()
    for ref in receipts:
        value = retained_json(files.read(ref))
        a2.EXPORT_OPERATION(value)
        key = value['pass'], value['operation'], value['object']['path']
        require(key in expected and key not in seen, 'OutputPopulation')
        seen.add(key)
        passed, operation = expected[key]
        require(value['object'] == operation['object'] and
            passed['start_us'] <= value['at_us'] < passed['end_us'], 'OutputPassInterval')
        files.read(value['object'])
        require(value['bytes'] == (0 if value['operation'] == 'delete' else value['object']['bytes']), 'artifact_gap')
        originals.append(value)
    require(seen == set(expected), 'OutputPopulation')
    approved = retained_json(files.read(plan['export_ceilings']))
    approval(files, plan['export_review'], plan['export_ceilings'])
    allowances = retained_json(files.read(plan['pre_allowances']))
    require(all(approved[k] == allowances[k] for k in
        ('export_passes', 'export_operations', 'export_bytes', 'export_write_bytes')), 'ExportCeilings')
    result = export_costs(compared, originals, approved=approved, include_partial=True)
    required_prior = planned.get('following_output_records', [])
    require(plan.get('following_output_records', []) == required_prior, 'FollowingOutputPopulation')
    require(not planned.get('previous_output_pass') or bool(required_prior), 'FollowingOutputPopulation')
    if required_prior:
        from trusted_router.async_settle_shadow_v2_pack_output_costs import following_totals
        carry = following_totals(files, [retained_json(files.read(ref))
            for ref in plan['following_output_records']], current_pass=passes[0]['pass'])
        for field, value in carry.items():
            result[field] += value
    reserved = dict(result)
    later = planned['later_passes']
    require(bool(later), 'MissingLaterOutputPlan')
    previous = max(p['pass'] for p in passes)
    for passed in later:
        require(passed['pass'] > previous and passed['depends_on'] == previous, 'LaterOutputOrder')
        require(set(passed['groups']) == {'coverage', 'roots', 'review', 'report'}, 'LaterOutputPopulation')
        previous = passed['pass']
        reserved['export_passes'] += 1
        for field in ('native_rpcs', 'export_operations', 'export_bytes', 'export_write_bytes'):
            require(type(passed[field]) is int and passed[field] >= 0, 'LaterOutputCost')
            reserved[field] += passed[field]
    require(all(reserved[key] <= approved[key] for key in
        ('native_rpcs', 'export_passes', 'export_operations', 'export_bytes', 'export_write_bytes')),
        'evidence_export_budget_gap')
    prefix = plan.get('output_prefix', '')
    files.obj(prefix + 'budget/export-next-pass-ceilings.json', reserved)
    files.obj(prefix + 'budget/export-compared-totals.json', result)
    return result


def compare_later_files(files: PackFiles, plan: dict[str, Any], references: list[dict[str, Any]], *,
                        plan_files: Any = None) -> dict[str, Any]:
    """Compare an independently reviewed individual output population."""
    from trusted_router.async_settle_shadow_v2_pack_output_costs import compare_files
    require('later_file_plan' in plan and 'later_file_review' in plan, 'MissingLaterFilePlan')
    source = files if plan_files is None else plan_files
    approval(source, plan['later_file_review'], plan['later_file_plan'])
    reservation = retained_json(source.read(plan['later_file_plan']))
    require(bool(reservation['reservations']), 'LaterOutputPopulation')
    measured = compare_files(files, reservation, references)
    return dict(plan=reservation, **measured)


def compare_passes(files: Any, spec: dict[str, Any], capture: dict[str, Any]) -> tuple[list[Any], list[dict[str, Any]], list[str]]:
    """The same independent pass population is required for selected and support days."""
    from trusted_router.async_settle_shadow_v2_pack_accounting_stream import (
        compare_export_accounting_streamed,
    )
    require(bool(capture['passes']) and len({p['id'] for p in capture['passes']})
            == len(capture['passes']), 'FinitePassPlan')
    require([p['id'] for p in capture['passes']] == spec['pass_ids'], 'FinitePassPlan')
    require(len({p['accounting']['path'] for p in capture['passes']}) == len(capture['passes']),
            'FinitePassPlan')
    require(any(all(p.get(key) == capture.get(key) for key in
        ('bundle', 'accounting', 'prefix', 'object_plan')) for p in capture['passes']), 'FinitePassPlan')
    comparisons = []
    accounting_roots = []
    accounting_missing: list[str] = []
    for passed in capture['passes']:
        accounting_roots.append(passed['accounting'])
        passed_bundle = (files.root / passed['bundle']).resolve()
        require(passed_bundle.is_relative_to(files.root.resolve()) and passed_bundle.is_file(),
                'MissingProducer:NativeExport:' + passed['id'])
        compared = compare_export_accounting_streamed(files, passed['accounting'], passed_bundle,
            day=spec['day'], object_plan=passed['object_plan'], prefix=passed.get('prefix', ''))
        comparisons.append(compared)
        accounting_missing.extend(passed['id'] + ':' + gap for gap in compared.missing)
    return comparisons, accounting_roots, accounting_missing


def compare_source_registrations(files: Any, spec: dict[str, Any]) -> None:
    """Every referenced capture original must be registered before selection."""
    from trusted_router.async_settle_shadow_v2_pack_stage import PRODUCERS, references
    registered = {ref['path']: ref for ref in spec.get('files', [])}
    require(len(registered) == len(spec.get('files', [])), 'SourceRegistration')
    seen: dict[str, dict[str, Any]] = {}
    for producer in PRODUCERS:
        require(bool(spec['captures'].get(producer)), 'MissingProducer:' + producer)
        pending = list(references(spec['captures'][producer]))
        while pending:
            ref = pending.pop()
            if ref['path'] in seen:
                require(seen[ref['path']] == ref, 'SourceRegistration:' + ref['path'])
                continue
            seen[ref['path']] = ref
            try:
                raw = files.read(ref)
            except (OSError, ValueError) as exc:
                raise ValueError('MissingProducer:' + producer + ':' + ref['path']) from exc
            require(registered.get(ref['path']) == ref, 'SourceRegistration:' + ref['path'])
            try:
                value = retained_json(raw)
            except ValueError:
                continue
            children = list(references(value))
            if type(value) is dict and value.get('format') == 'shadow-v2-live-export-a3-n':
                prefix = ref['path'].rsplit('/', 1)[0] + '/' if '/' in ref['path'] else ''
                children = [{**child, 'path': prefix + child['path']} for child in children]
            pending.extend(children)
