"""A2 byte graph for P5 test bundles; no frozen fixture is modified."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Any

from scripts.async_settle.pilot_enablement import PILOT, roster_coverage
from trusted_router import async_settle_shadow_v2_a1_evidence as a1
from trusted_router import async_settle_shadow_v2_a2_contract as c
from trusted_router import async_settle_shadow_v2_a2_evidence as e
from trusted_router import async_settle_shadow_v2_a2_roots as r
from trusted_router import async_settle_shadow_v2_evidence as p1
from trusted_router.async_settle_shadow_v2_gate_bundle import ATTACHMENTS, Context
from trusted_router.async_settle_shadow_v2_gate_failures import TAG, detail
from trusted_router.async_settle_shadow_v2_gate_inputs import Bundle, Preimage, Row
from trusted_router.async_settle_shadow_v2_gate_preimages import preimages
from trusted_router.async_settle_shadow_v2_schema import day_at, sha256
from trusted_router.detached_jws import canonical

ROOT = Path(__file__).parent / 'fixtures/async_settlement'


@cache
def fixture_bytes(file: str, name: str) -> bytes:
    values = json.loads((ROOT / 'shadow_v2_a2' / (file + '.json')).read_bytes())['variants']
    return canonical(next(v['object'] for v in values if v['name'] == name))


def seal(bundle: Bundle) -> Bundle:
    assert bundle.budget and bundle.inventory and bundle.proof
    artifacts: dict[str, bytes] = {}
    def ref(path: str, raw: bytes) -> dict[str, Any]:
        artifacts[path] = raw
        return dict(path=path, sha256=r.file_digest(raw), bytes=len(raw))
    def approval(reference: dict[str, Any]) -> dict[str, Any]:
        record = dict(**TAG, subject_sha256=reference['sha256'], reviewer='fixture-reviewer',
                      reviewed_at_us=bundle.completed_at_us, decision='approved')
        return {k: v for k, v in record.items() if k not in TAG} | dict(record=ref('reviews/' + reference['sha256'], e.REVIEW_RECORD.serialize(record)))
    empty, _ = detail([], 'files')
    config = ref('config/probe.json', canonical(dict(workspace_count=1, eligible_only=True, supported_candidates=1,
        fallbacks=False, routes=['chat.completions', 'responses'])))
    source = ref('sources/pilot_enablement.py', (Path(__file__).parents[1] / 'scripts/async_settle/pilot_enablement.py').read_bytes())
    # All retained inputs/outputs remain their original shape and bytes.
    payload = ref('budgets/pre-input.json', canonical(bundle.budget.pre_input))
    from scripts.async_settle.pilot_enablement import fleet_budgets_pre
    output = ref('budgets/pre-output.json', canonical(fleet_budgets_pre(bundle.budget.pre_input)))
    dependency_refs = []
    for name in ('deployment', 'monitoring', 'read_health'):
        value = bundle.budget.pre_input[name]
        dependency_refs.append(ref('budgets/' + name + '.json', Path(value['path']).read_bytes()))
    dependencies, pages = detail(sorted(dependency_refs, key=lambda v: v['path']), 'files')
    for page in pages:
        ref('details/budget-dependencies/' + page['page_seq'], canonical(page))
    delegated = dict(**TAG, entry_point='pilot_enablement.fleet_budgets_pre', payload=payload, dependencies=dependencies, source=source)
    limit = ref('budgets/limits.json', canonical(dict(regions=[], fleet=dict.fromkeys(e.PEAKS.fields))))
    load = dict(**TAG, interval=dict(start_us=min(b.start_us for b in bundle.inventory.boots), end_us=bundle.completed_at_us),
                segments=empty, operations=empty, limits=limit, approval=approval(limit))
    pre = dict(**TAG, stage='pre', retained=delegated, output=output, pre_envelope_sha256=None,
               approval=approval(payload), v2_load=load)
    post = pre | dict(stage='post', pre_envelope_sha256=sha256(pre),
                retained={**delegated, 'entry_point': 'pilot_enablement.fleet_budgets'})
    policy = dict(**TAG, workspace_fingerprints=[sha256(dict(workspace='dedicated-fixture'))],
        regional_seed_policy='each_traffic_region', required_regions=['us-central1'], primary_region='us-central1',
        maximum_publishers=1, configuration=config, approval=approval(config))
    for field, value, schema in (('pre_budget_sha256', pre, e.BUDGET_ENVELOPE),
        ('post_budget_sha256', post, e.BUDGET_ENVELOPE), ('cohort_seed_policy_sha256', policy, e.COHORT_SEED_POLICY)):
        artifacts['proof.' + field] = schema.serialize(value)
    shutdown = [e.SHUTDOWN_ENTRY.parse(row.raw) for row in bundle.attachments if row.key.kind == 'shutdown_entry']
    shutdown_ref, shutdown_pages = detail(sorted(shutdown, key=lambda v: (v['day'], v['boot'])), 'shutdown_writers')
    for page in shutdown_pages:
        ref('details/shutdown/' + page['page_seq'], canonical(page))
    for row in bundle.rows:
        if row.key.kind in (a1.KINDS[-1], p1.KINDS[4]):
            ref('rows/' + ('close/' if row.key.kind == a1.KINDS[-1] else 'closure/') + row.key.id, row.raw)
    for row in bundle.attachments:
        if row.key.kind == 'shutdown_measurement':
            ref(row.key.id, row.raw)
    measurement_file = ref('measurements/inventory.json', canonical([v['measurement'] for v in shutdown]))
    artifacts['proof.shutdown_inventory_sha256'] = e.SHUTDOWN_INVENTORY.serialize(dict(**TAG,
        writers=shutdown_ref, measurements=measurement_file, approval=approval(measurement_file), complete=True))
    reset_payload = ref('resets/inputs.json', canonical(dict(rows=[], days=list(bundle.days), proof={})))
    reset_source = ref('sources/shadow_report.py', (Path(__file__).parents[1] / 'scripts/async_settle/shadow_report.py').read_bytes())
    artifacts['proof.resolution_dag_sha256'] = e.RESOLUTION_DAG.serialize(dict(**TAG,
        retained=dict(**TAG, entry_point='shadow_report.collect_rollbacks', payload=reset_payload, dependencies=empty, source=reset_source),
        history=empty, approvals=[]))
    external = dict(**TAG, requirements=[], pilot=None)
    artifacts['proof.external_proofs_sha256'] = e.EXTERNAL_PROOFS.serialize(external)
    for field, file in (('evaluator_fixture_sha256', 'billing_v1.json'), ('binding_fixture_sha256', 'shadow_v1.json'), ('wire_fixture_sha256', 'shadow_v2/manifest.json')):
        artifacts['proof.' + field] = (ROOT / file).read_bytes()
    manifests = []
    for row in bundle.rows:
        if row.key.kind != p1.KINDS[5]:
            continue
        manifest = a1.MANIFEST.parse(row.raw)
        day = manifest['day']
        serving = []
        for boot in bundle.inventory.boots:
            end = boot.end_us or bundle.completed_at_us
            if not day_at(boot.start_us) <= day <= day_at(end - 1):
                continue
            midnight = (dt.date.fromisoformat(day) - dt.date(1970, 1, 1)).days * 86400000000
            pins = (dict(TR_ASYNC_SETTLE_ENABLED='false', TR_ASYNC_SETTLE_PROTECTION='false',
                TR_ASYNC_SETTLE_SHADOW_WORKSPACES=PILOT, TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED='false')
                if boot.role == 'router' else dict(TR_ASYNC_SETTLE_NEGOTIATE='off', TR_ASYNC_SETTLE_SHADOW='on'))
            serving.append(dict(instance=boot.boot, role=boot.role, revision=boot.revision, region=boot.region,
                intervals=[dict(day=day, started_at_us=max(midnight, boot.start_us),
                    flushed_at_us=min(midnight + 86400000000, end))], pins=pins))
        roster_coverage(serving)
        roster = ref('inventory/' + day + '/serving.json', canonical(serving))
        deployment = ref('inventory/' + day + '/deployment.json', canonical(dict(serving=serving,
            maximum_router_instances_by_region=dict(bundle.budget.regional_caps))))
        independent = dict(**TAG, day=day, serving=roster, deployment=deployment, approval=approval(deployment), complete=True)
        artifacts[day + '/manifest.independent_inventory_sha256'] = e.INDEPENDENT_INVENTORY.serialize(independent)
        manifest['independent_inventory_sha256'] = sha256(independent)
        for field, raw in (('config_sha256', artifacts[config['path']]), ('fixture_sha256', (ROOT / 'shadow_v2_a2/manifest.json').read_bytes())):
            artifacts[day + '/manifest.' + field] = raw
            manifest[field] = r.file_digest(raw)
        manifests.append((row.key, manifest))
    proof = a1.PROOF_MANIFEST.parse(bundle.proof)
    for field, schema in c.OBJECT_PREIMAGES.items():
        if field.startswith('proof.'):
            proof[field.split('.')[1]] = sha256(schema.parse(artifacts[field]))
    for field in c.FILE_PREIMAGES:
        if field.startswith('proof.'):
            proof[field.split('.')[1]] = r.file_digest(artifacts[field])
    ctx = Context(replace(bundle, preimages=tuple(Preimage(k, v) for k, v in artifacts.items())), None, 'pre-flip')
    # Root construction only consumes decoded coverage containers. The generator
    # already called every trace constructor; evaluation validates those bytes.
    ctx.attachments = {row.key: ATTACHMENTS[row.key.kind].parse(row.raw)
                       for row in bundle.attachments if row.key.kind == 'coverage'}
    for _, manifest in manifests:
        _, roots = preimages(ctx, manifest)
        for field, stream in c.ENTRY_PREIMAGES.items():
            owner, key = field.split('.')
            (manifest if owner == 'manifest' else proof)[key] = r.entry_root([r._root_entry(x['key'], x['bodies']) for x in roots[field]], stream=stream)
        for field, stream in c.CROSS_DAY_PREIMAGES.items():
            proof[field.split('.')[1]] = r.entry_root([dict(day=v['day'], root=a1.coverage_root(v['pages']), count=sum(len(p['entries']) for p in v['pages'])) for v in roots[field]], stream=stream + '_root')
    raw_proof = a1.PROOF_MANIFEST.serialize(proof)
    rows = [row for row in bundle.rows if row.key.kind != p1.KINDS[5]]
    for key, manifest in manifests:
        manifest['proof_manifest_sha256'] = r.file_digest(raw_proof)
        rows.append(Row(key, a1.MANIFEST.serialize(manifest)))
    from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
    return replace(bundle, rows=tuple(rows), proof=raw_proof,
        preimages=tuple(Preimage(k, v) for k, v in artifacts.items() if k not in RELEASE_FILES))
