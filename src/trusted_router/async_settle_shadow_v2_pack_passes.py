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
    if plan.get('following_output_records'):
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


def compare_later_files(files: PackFiles, plan: dict[str, Any], references: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare individual staged files before the next local output pass.

    A supplied reservation record is checked unchanged. Older plans reserve a
    finite group ceiling; expand it into named file reservations before reading
    the emitted bytes. Return receipts to the caller for the following pass.
    """
    from trusted_router.async_settle_shadow_v2_pack_output_costs import compare_files, reserve_files
    planned = retained_json(files.read(plan['output_plan']))
    later = planned['later_passes']
    limits = {field: sum(p[field] for p in later) for field in
        ('export_operations', 'export_bytes', 'export_write_bytes', 'native_rpcs')}
    reservation = (retained_json(files.read(plan['later_file_plan'])) if 'later_file_plan' in plan
        else reserve_files(references, source_pass=max(p['pass'] for p in planned['passes']), ceiling=limits))
    measured = compare_files(files, reservation, references)
    return dict(plan=reservation, **measured)
