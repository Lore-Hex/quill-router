"""Section 15.7 original-byte cases, retained for the base-loader handoff."""
from __future__ import annotations

import copy
import json
from typing import Any

from trusted_router.async_settle_shadow_v2_collect_ports import CHECKS, SOURCES
from trusted_router.detached_jws import canonical

RANGE_CASES = ('middle_range_page', 'last_range_page', 'range_eof', 'entire_kind', 'entire_day',
               'accounting_file', 'original_hash', 'original_bytes', 'object_generation', 'extra_native_rpc', 'extra_rerun')
STAGE_CASES = ('idle_boot', 'killed_boot', 'false_shutdown_zero', 'pre_budget', 'post_budget',
               'cap_only_manifest', 'resolution_support', 'new_booking_region',
               'required_criterion_moved', 'deferred_name_removed', 'earlier_stage_result')
SEED_PREREQUISITES = (*CHECKS, 'stage_transport', 'fleet_budgets_pre', 'fleet_budgets')
# A named set is written into every run's case index, so an omitted case is visible.
CASE_NAMES = (*RANGE_CASES, *STAGE_CASES,
    *('criterion_' + name for name in CHECKS),
    *('source_' + criterion + '_' + source for criterion, sources in SOURCES.items() for source in sources),
    *('seed_' + name + '_' + boundary for name in SEED_PREREQUISITES for boundary in ('before', 'at')))


def negative_bundle(original: dict[str, Any], name: str) -> dict[str, Any]:
    """Change original records, never pass an already-decoded gate input."""
    assert name in CASE_NAMES
    value = copy.deepcopy(original)
    preimages = value['preimages']
    def find(path):
        found = [p for p in preimages if path(p['name'])]
        assert found, name
        return found[0]
    def edit(item, change):
        body = json.loads(item['raw'])
        change(body)
        item['raw'] = canonical(body).decode()
    if name in ('entire_kind', 'entire_day', 'cap_only_manifest'):
        if name == 'entire_kind':
            value['rows'] = [r for r in value['rows'] if 'counter' not in r['kind']]
        elif name == 'entire_day':
            value['rows'] = [r for r in value['rows'] if r['id'].endswith('/cap-v2')]
        else:
            value['rows'] = [r for r in value['rows'] if not r['id'].endswith('/manifest-v2')]
    elif name == 'accounting_file':
        preimages.remove(find(lambda p: p == 'native-export/accounting.json'))
    elif name in ('middle_range_page', 'last_range_page', 'range_eof', 'extra_native_rpc', 'extra_rerun'):
        item = find(lambda p: 'native-export/' in p and '/reads/' in p)
        def change(body):
            entries = body['entries']
            if name in ('middle_range_page', 'last_range_page'):
                entries.pop(1 if name == 'middle_range_page' else -1)
            elif name == 'range_eof':
                entries[-1]['complete'] = False
            elif name == 'extra_native_rpc':
                entries[0]['native_rpcs'] += 1
            else:
                entries.append(copy.deepcopy(entries[0]))
        edit(item, change)
    elif name in ('original_hash', 'original_bytes', 'object_generation'):
        item = find(lambda p: 'native-export/' in p and '/entries/' in p)
        def native(body):
            obj = next(e for e in body['entries'] if 'generation' in e['metadata']) if name == 'object_generation' else body['entries'][0]
            if name == 'original_hash':
                obj['sha256'] = '0' * 64
            elif name == 'original_bytes':
                obj['bytes'] += 1
            else:
                obj['metadata']['generation'] = '8'
        edit(item, native)
    elif name in ('idle_boot', 'killed_boot'):
        item = find(lambda p: p.startswith('inventory/') and p.endswith('/serving.json'))
        def boot(body):
            extra = copy.deepcopy(body[0])
            extra.update(instance=name)
            body.append(extra)
        edit(item, boot)
    elif name.startswith('criterion_'):
        criterion = name.removeprefix('criterion_')
        preimages.remove(find(lambda p: p.startswith('readiness/') and p.endswith('/' + criterion + '.json')))
    elif name.startswith('source_'):
        criterion, source = next((c, s) for c, sources in SOURCES.items() for s in sources
                                 if name == 'source_' + c + '_' + s)
        preimages.remove(find(lambda p: p.startswith('readiness/') and p.endswith('/' + criterion + '-' + source + '.json')))
    elif name == 'false_shutdown_zero':
        item = find(lambda p: p.startswith('shutdown/') and p.endswith('/measurement.json'))
        edit(item, lambda body: body.update(result_us=0))
    elif name in ('pre_budget', 'post_budget'):
        stage = name.split('_')[0]
        preimages.remove(find(lambda p: p == 'proof.' + stage + '_budget_sha256'))
    elif name == 'resolution_support':
        preimages.remove(find(lambda p: p == 'old/reviewed'))
    elif name.startswith('seed_'):
        prerequisite, boundary = name[5:].rsplit('_', 1)
        if prerequisite in CHECKS:
            ledger = json.loads(find(lambda p: p.startswith('readiness/') and p.endswith('/ledger.json'))['raw'])
            at = ledger[prerequisite]['completed_at_us']
        else:
            at = value['completed_at_us']
        row = next(r for r in value['rows'] if 'sample' in r['kind'])
        edit(row, lambda body: body.update(observed_at_us=at - (boundary == 'before')))
    elif name == 'new_booking_region':
        row = next(r for r in value['rows'] if 'sample' in r['kind'])
        edit(row, lambda body: body['deployment'].update(region='new-unseeded-region'))
    else:
        item = find(lambda p: p == 'loading/release-plan.json')
        def policy(body):
            if name == 'required_criterion_moved':
                criterion = body['required_stages']['transport'].pop()
                body['required_stages'].setdefault('pre-flip', []).append(criterion)
            elif name == 'deferred_name_removed':
                body['required_stages']['pre-flip'].remove('d2_d3_capacity_slo')
            else:
                body['required_stages']['pre-flip'] = ['policy_rollback_status']
                body['criteria']['policy_rollback_status'] = ['shadow_optout', 'close_retention', 'retained_losses']
        edit(item, policy)
    assert canonical(value) != canonical(original), name
    return value


def stage_ports(original):
    from scripts.async_settle.shadow_report import shadow_v2_ports
    from trusted_router.async_settle_shadow_v2_a2_roots import file_digest
    from trusted_router.async_settle_shadow_v2_gate_release import decode_release_plan
    raw = next(p['raw'].encode() for p in original['preimages'] if p['name'] == 'loading/release-plan.json')
    release = decode_release_plan(raw, file_digest(raw))
    return shadow_v2_ports(b'[]', b'[]', release=release)


def requested_stage(case):
    if case.startswith('seed_') or case == 'new_booking_region':
        return 'shadow-serving'
    if case == 'earlier_stage_result':
        return 'pre-flip'
    if case == 'pre_budget':
        return 'fleet-budgets-pre'
    if case == 'post_budget':
        return 'fleet-budgets'
    return 'transport'
