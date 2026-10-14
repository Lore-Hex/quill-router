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
    originals = []
    for ref in receipts:
        value = retained_json(files.read(ref))
        a2.EXPORT_OPERATION(value)
        files.read(value['object'])
        require(value['bytes'] == (0 if value['operation'] == 'delete' else value['object']['bytes']), 'artifact_gap')
        originals.append(value)
    approved = retained_json(files.read(plan['export_ceilings']))
    approval(files, plan['export_review'], plan['export_ceilings'])
    allowances = retained_json(files.read(plan['pre_allowances']))
    require(all(approved[k] == allowances[k] for k in
        ('export_passes', 'export_operations', 'export_bytes', 'export_write_bytes')), 'ExportCeilings')
    result = export_costs(compared, originals, approved=approved, include_partial=True)
    files.obj('budget/export-compared-totals.json', result)
    return result
