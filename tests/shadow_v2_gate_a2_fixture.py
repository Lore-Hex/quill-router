"""A2 byte graph for P5 test bundles; no frozen fixture is modified."""
from __future__ import annotations

import copy
import datetime as dt
import json
from dataclasses import replace
from functools import cache, lru_cache
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
from trusted_router.async_settle_shadow_v2_gate_preimages import _entry_digest, preimages
from trusted_router.async_settle_shadow_v2_schema import day_at, sha256
from trusted_router.detached_jws import canonical

ROOT = Path(__file__).parent / 'fixtures/async_settlement'


@cache
def fixture_bytes(file: str, name: str) -> bytes:
    values = json.loads((ROOT / 'shadow_v2_a2' / (file + '.json')).read_bytes())['variants']
    return canonical(next(v['object'] for v in values if v['name'] == name))


def seal(bundle: Bundle, *, commit: bool = True) -> Bundle:
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
    if not commit:
        # Direct original family: envelopes now, one final commitment pass after
        # retained sources, captures and synthetic approvals have been generated.
        return replace(bundle, preimages=tuple(Preimage(k, v) for k, v in artifacts.items()))
    proof = a1.PROOF_MANIFEST.parse(bundle.proof)
    for field, schema in c.OBJECT_PREIMAGES.items():
        if field.startswith('proof.'):
            proof[field.split('.')[1]] = sha256(schema.parse(artifacts[field]))
    for field in c.FILE_PREIMAGES:
        if field.startswith('proof.'):
            proof[field.split('.')[1]] = r.file_digest(artifacts[field])
    ctx = Context(replace(bundle, preimages=tuple(Preimage(k, v) for k, v in artifacts.items())), None, 'pre-flip')
    ctx.rows = {row.key: a1.validate_row(row.key.kind, row.key.id, row.raw) for row in bundle.rows}
    # Root construction only consumes decoded coverage containers. The generator
    # already called every trace constructor; evaluation validates those bytes.
    ctx.attachments = {row.key: ATTACHMENTS[row.key.kind].parse(row.raw)
                       for row in bundle.attachments if row.key.kind == 'coverage'}
    for _, manifest in manifests:
        _, roots = preimages(ctx, manifest)
        for field, stream in c.ENTRY_PREIMAGES.items():
            owner, key = field.split('.')
            (manifest if owner == 'manifest' else proof)[key] = _entry_digest(stream, tuple((x['key'], tuple(x['bodies'])) for x in roots[field]))
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


def transport(bundle: Bundle) -> bytes:
    """Only original bytes cross the production loading boundary."""
    def rows(values: tuple[Row, ...]) -> list[dict[str, Any]]:
        return [dict(kind=r.key.kind, id=r.key.id, raw=r.raw.decode()) for r in values]
    return canonical(dict(policy_version='shadow-v2', contract_revision='a1',
        rows=rows(bundle.rows), attachments=rows(bundle.attachments), proof=bundle.proof.decode() if bundle.proof else None,
        ledgers=[raw.decode() for raw in bundle.ledgers], days=list(bundle.days), completed_at_us=bundle.completed_at_us,
        preimages=[dict(name=p.name, raw=p.raw.decode()) for p in bundle.preimages],
        **{key: getattr(bundle, key).decode() if getattr(bundle, key) else None for key in ('transport', 'fleet', 'pre', 'serving')}))


def commit_originals(bundle: Bundle, artifacts: dict[str, bytes]) -> Bundle:
    """Commit the completed original-byte graph exactly once."""
    assert bundle.proof is not None
    proof = a1.PROOF_MANIFEST.parse(bundle.proof)
    proof['attachments'] = [dict(sha256=r.file_digest(row.raw), bytes=len(row.raw))
                           for row in bundle.attachments if row.key.kind == 'coverage']
    for field, schema in c.OBJECT_PREIMAGES.items():
        if field.startswith('proof.'):
            proof[field.split('.')[1]] = sha256(schema.parse(artifacts[field]))
    from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
    for field, path in RELEASE_FILES.items():
        proof[field.split('.')[1]] = r.file_digest((Path(__file__).parents[1] / path).read_bytes())
    current = replace(bundle, preimages=tuple(Preimage(k, v) for k, v in artifacts.items()))
    ctx = Context(current, None, 'pre-flip')
    # Construction already owns this complete population. Do not validate each
    # generated slot merely to enumerate root keys; the loader does that once.
    trace_rows = tuple(row for row in current.attachments if row.key.kind == 'publisher_trace')
    ctx.trace_population_cache = ((trace_rows, current.inventory, current.completed_at_us),
                                  frozenset(row.key.id for row in trace_rows))
    ctx.rows = {row.key: a1.validate_row(row.key.kind, row.key.id, row.raw) for row in bundle.rows}
    ctx.attachments = {row.key: ATTACHMENTS[row.key.kind].parse(row.raw)
                       for row in bundle.attachments if row.key.kind == 'coverage'}
    manifests = []
    for row in bundle.rows:
        if row.key.kind != p1.KINDS[5]:
            continue
        manifest = a1.MANIFEST.parse(row.raw)
        day = manifest['day']
        independent = e.INDEPENDENT_INVENTORY.parse(artifacts[day + '/manifest.independent_inventory_sha256'])
        manifest['independent_inventory_sha256'] = sha256(independent)
        for key in ('config_sha256', 'fixture_sha256'):
            manifest[key] = r.file_digest(artifacts[day + '/manifest.' + key])
        _, inputs = preimages(ctx, manifest)
        for field, stream in c.ENTRY_PREIMAGES.items():
            owner, key = field.split('.')
            (manifest if owner == 'manifest' else proof)[key] = _entry_digest(stream, tuple((x['key'], tuple(x['bodies'])) for x in inputs[field]))
        for field, stream in c.CROSS_DAY_PREIMAGES.items():
            proof[field.split('.')[1]] = r.entry_root([dict(day=v['day'], root=a1.coverage_root(v['pages']),
                count=sum(len(p['entries']) for p in v['pages'])) for v in inputs[field]], stream=stream + '_root')
        manifests.append((row.key, manifest))
    raw_proof = a1.PROOF_MANIFEST.serialize(proof)
    rows = [row for row in bundle.rows if row.key.kind != p1.KINDS[5]]
    for key, manifest in manifests:
        manifest['proof_manifest_sha256'] = r.file_digest(raw_proof)
        rows.append(Row(key, a1.MANIFEST.serialize(manifest)))
    return replace(current, rows=tuple(rows), proof=raw_proof)


def real_fixture(tmp_path: Path, *, seconds: int = 20, pilot_stage: str | None = None, empty_segment: bool = False, variant: str | None = None, churn: bool = False, start_us: int | None = None, chain: bool = True, traffic: bool = True, recovered_checkpoint: bool = False) -> tuple[Bundle, Any, dict[str, Any]]:
    """Complete small original-byte fixture, with real retained function outputs.

    The release authority is a test trust root separate from the export. Every
    pin is computed over original files; no hand-written digest is evidence.
    """
    from dataclasses import asdict
    from fractions import Fraction

    from scripts.async_settle import pilot_enablement as pilot
    from scripts.async_settle import shadow_report as legacy
    from scripts.async_settle.async_settle_shadow_v2_gate_tools import run_retained
    from tests.shadow_v2_gate_fixture_generator import (
        REGION,
        REV,
        START,
        build,
        pre_enable_bundle,
    )
    from tests.shadow_v2_gate_fixture_generator import (
        base_ports as ports,
    )
    from trusted_router.async_settle_shadow_evidence import Counters
    from trusted_router.async_settle_shadow_v2_gate_inputs import (
        ALLOWANCES,
        BUDGET_MEASUREMENTS,
        COMPONENTS,
        SHUTDOWN_SOURCE,
        ReleasePlan,
    )
    from trusted_router.async_settle_shadow_v2_gate_views import (
        EXTERNAL_REQUIRED,
        MEASUREMENTS_PATH,
    )

    anchor = 1791243800000000  # Shared reviewed approval, independent of observation start.
    START = START if start_us is None else start_us
    bundle = build(tmp_path, churn=churn, seconds=seconds, original_sources=True, start_us=START, traffic=traffic, recovered_checkpoint=recovered_checkpoint,
        overlapping=variant == 'simultaneous_capacity_gap', health_damage=variant)
    assert bundle.inventory and bundle.budget
    end = bundle.completed_at_us
    from trusted_router.async_settle_shadow_v2_gate_preimages import RELEASE_FILES
    artifacts = {p.name: p.raw for p in bundle.preimages if p.name not in RELEASE_FILES}
    def ref(path: str, raw: bytes) -> dict[str, Any]:
        artifacts[path] = raw
        return dict(path=path, sha256=r.file_digest(raw), bytes=len(raw))
    def approve(reference: dict[str, Any], at: int = end) -> dict[str, Any]:
        record = dict(**TAG, subject_sha256=reference['sha256'], reviewer='fixture-reviewer', reviewed_at_us=at, decision='approved')
        return {k: v for k, v in record.items() if k not in TAG} | dict(record=ref('reviews/' + reference['sha256'] + '/' + str(at), e.REVIEW_RECORD.serialize(record)))
    def stream(values: list[dict[str, Any]], name: str, path: str) -> dict[str, Any]:
        values = sorted(values, key=lambda v: r._key(v, name))
        desc, pages = detail(values, name)
        for page in pages:
            ref(path + '/' + page['page_seq'], canonical(page))
        return desc
    def rat(value: int | float | Fraction) -> dict[str, int]:
        value = Fraction(value)
        return dict(numerator=value.numerator, denominator=value.denominator)

    key_ports = ports()
    tool_ports = legacy.shadow_v2_ports(key_ports.approvals[0].keyset, key_ports.approvals[1].keyset)
    sources = {entry: ref('sources/' + entry.split('.')[0] + '.py',
               next(a.source for a in tool_ports.approvals if a.entry_point.endswith('.' + entry)))
               for entry in e.RETAINED_ENTRY_POINTS}
    def delegated(entry: str, payload: Any, dependencies: list[dict[str, Any]], path: str) -> tuple[dict[str, Any], Any]:
        result = run_retained(entry, payload, {d['path']: artifacts[d['path']] for d in dependencies})
        wrapper = dict(**TAG, entry_point=entry, payload=ref(path + '/input.json', canonical(payload)),
            dependencies=stream(sorted(dependencies, key=lambda d: d['path']), 'files', path + '/deps'), source=sources[entry])
        from trusted_router.async_settle_shadow_v2_gate_retained import (
            RETAINED_OUTPUT,
            capture_path,
        )
        output = ref(path + '/captured-output.json', result.output)
        capture = dict(entry_point=entry, payload_sha256=wrapper['payload']['sha256'], output=output)
        ref(capture_path(wrapper['payload']['sha256']), RETAINED_OUTPUT.serialize(capture))
        return wrapper, result

    pre_input = pre_enable_bundle(tmp_path)
    # Dormant measurement ends before the first shadow writer starts.
    pre_input.update(started_at_us=anchor - 60000001, flushed_at_us=anchor - 1)
    for row in pre_input['serving']:
        row['intervals'] = [dict(day=day_at(anchor - 1), started_at_us=anchor - 60000001, flushed_at_us=anchor - 1)]
    dependencies = []
    for key in ('deployment', 'monitoring', 'read_health'):
        value = json.loads(Path(pre_input[key]['path']).read_bytes())
        if key == 'deployment':
            value['serving'] = pre_input['serving']
        else:
            value.update(started_at_us=pre_input['started_at_us'], flushed_at_us=pre_input['flushed_at_us'])
        dep = ref('budgets/' + key + '.json', canonical(value))
        dependencies.append(dep)
        pre_input[key] = {k: dep[k] for k in ('path', 'sha256')}
    pre_retained, pre_call = delegated('pilot_enablement.fleet_budgets_pre', pre_input, dependencies, 'budgets/pre')
    pre_output = json.loads(pre_call.output)
    assert pre_output['status'] == 'PASS'
    completed_pre = ref('budgets/pre-completed.json', canonical(pre_output | dict(completed_at_us=anchor - 1)))
    serving = []
    for path, raw in artifacts.copy().items():
        if path.endswith('/serving.json'):
            serving.extend(json.loads(raw))
    merged_serving = {}
    for row in serving:
        if row['instance'] not in merged_serving:
            merged_serving[row['instance']] = {**row, 'intervals': []}
        merged_serving[row['instance']]['intervals'].extend(row['intervals'])
    serving = list(merged_serving.values())
    counter = Counters(REGION, REV, clock=lambda: START / 1000000)
    counter.instance = '00000000-0000-4000-8000-000000000001'
    counter._day()
    _, legacy_body = counter.snapshot(closed=True)[0]
    legacy_boot = legacy_body['instance']
    legacy_serving = [{**row, 'instance': legacy_boot if row['role'] == 'router' else row['instance']} for row in serving]
    combined = {}
    for row in legacy_serving:
        if row['instance'] not in combined:
            combined[row['instance']] = {**row, 'intervals': []}
        for span in row['intervals']:
            if span not in combined[row['instance']]['intervals']:
                combined[row['instance']]['intervals'].append(span)
    legacy_serving = list(combined.values())
    legacy_body.update(started_at_us=START, flushed_at_us=end)
    legacy_body['admission_observer'].update(workspace_reads=5, health_reads=20)
    legacy_rows = [dict(kind=pilot.COUNTER, id=day_at(START) + '/' + legacy_boot, body=legacy_body)]
    post_input = dict(serving=legacy_serving, rows=legacy_rows, workspace_count=1,
        router_instance_ids=[legacy_boot], maximum_router_instances=1,
        maximum_router_instances_by_region={REGION: 1}, regional_budgets=pre_output['regional_budgets'],
        approved_fleet_reads_per_second=pre_output['approved_fleet_reads_per_second'],
        pre_enable={k: completed_pre[k] for k in ('path', 'sha256')},
        headroom=pre_input['monitoring'], freshness_maxima_seconds=dict(
            publisher_period=2., publisher_jitter=.25, publication=.5, poll_period=1.,
            poll_jitter=.25, install=.25, skew=.25, workspace_period=4., workspace_jitter=.25, workspace_install=.5))
    measured = {k: rat(getattr(bundle.budget, k)) for k in ALLOWANCES}
    from trusted_router.async_settle_shadow_v2_gate_inputs import TOTALS
    measured.update({k: getattr(bundle.budget, k) for k in TOTALS})
    measured['component_us'] = {k: int(bundle.budget.component_seconds[k] * 1000000) for k in COMPONENTS}
    if variant == 'freshness_budget_gap':
        measured['component_us']['publication'] = 500001
        post_input['freshness_maxima_seconds']['publication'] = 0.500001
    elif variant == 'evidence_export_budget_gap':
        measured['export_operations'] = 0
    measurement_ref = ref(MEASUREMENTS_PATH, BUDGET_MEASUREMENTS.serialize(measured))
    dependencies_post = [completed_pre, dependencies[1], measurement_ref]
    post_retained, post_call = delegated('pilot_enablement.fleet_budgets', post_input, dependencies_post, 'budgets/post')

    limits = dict(regions=[dict(region=REGION, approved={k: (0 if k in ('startup_operations_1s', 'close_operations_1s') else 1)
        if k.endswith('_routers') or k.endswith('_publishers') or k.endswith('_1s') else rat(1000)
        for k in e.PEAKS.fields})], fleet={k: (1 if k.endswith('_routers') or k.endswith('_publishers') or k.endswith('_1s') else rat(1000)) for k in e.PEAKS.fields})
    limits['fleet']['cpu_percent'] = rat(44)
    for value in [limits['fleet'], limits['regions'][0]['approved']]:
        value['reads_per_second'] = rat(10)
        value['configured_reads_per_second'] = rat(10)
        value['pending_rows_per_second'] = rat(300)
        value['configured_pending_rows_per_second'] = rat(300)
    for value in [limits['fleet'], limits['regions'][0]['approved']]:
        value.update(startup_operations_1s=1, close_operations_1s=2, export_operations_1s=2,
                     export_bytes_per_second=rat(30000))
    limit_ref = ref('budgets/limits.json', canonical(limits))
    segments = []
    spans = [(boot, rate.start_us, rate.end_us) for boot in bundle.inventory.boots
             for rate in bundle.budget.rates if rate.boot == boot.boot]
    spans += [(boot, max(boot.start_us, epoch * 86400000000), min(boot.end_us, (epoch + 1) * 86400000000))
        for boot in bundle.inventory.boots if boot.role == 'enclave'
        for epoch in range(boot.start_us // 86400000000, (boot.end_us - 1) // 86400000000 + 1)]
    for boot, left, right in sorted(spans, key=lambda v: (v[0].boot, v[1])):
        peaks = dict.fromkeys(e.PEAKS.fields)
        peaks.update(startup_operations_1s=0, close_operations_1s=2, export_operations_1s=2,
            export_operations_per_second=rat(Fraction(2, 5)), export_bytes_per_second=rat(Fraction(131072, 5)))
        peaks.update(simultaneous_routers=int(boot.role == 'router'), simultaneous_publishers=int(boot.contender),
            reads_per_second=rat(Fraction(13, 4) if boot.role == 'router' else 0),
            pending_rows_per_second=rat(Fraction(1001, 4) if boot.role == 'router' else 0),
            claim_operations_per_second=rat(Fraction(1, 2) if boot.role == 'router' else 0),
            scan_operations_per_second=rat(Fraction(1, 2) if boot.role == 'router' else 0),
            publication_operations_per_second=rat(Fraction(1, 2) if boot.role == 'router' else 0),
            trust_rows_per_second=rat(Fraction(1, 4) if boot.role == 'router' else 0),
            health_rows_per_second=rat(1 if boot.role == 'router' else 0))
        if boot.role == 'router':
            counter_value = next(a1.COUNTER.parse(row.raw) for row in bundle.rows
                if row.key.kind == p1.KINDS[3] and row.key.id == day_at(left) + '/' + boot.boot)
            obs, pub = counter_value['admission_observer'], counter_value['publisher']
            duration = Fraction(right - left, 1000000)
            counts = dict(reads_per_second=obs['workspace_reads'] + obs['health_reads'] + pub['claim_attempts'] + 3 * pub['scans'],
                pending_rows_per_second=obs['workspace_reads'] * 1001,
                trust_rows_per_second=obs['workspace_reads'], health_rows_per_second=obs['health_reads'],
                claim_operations_per_second=pub['claim_attempts'], scan_operations_per_second=pub['scans'],
                publication_operations_per_second=pub['publications'])
            peaks.update({field: rat(count / duration) for field, count in counts.items()})
        segments.append(dict(day=day_at(left), boot=boot.boot, region=boot.region,
            start_us=left, end_us=right, role=boot.role, contender=boot.contender, workspace_count=1, peaks=peaks))
    if empty_segment:
        source = next(s for s in segments if s['role'] == 'router')
        segments.append({**source, 'start_us': source['end_us'], 'day': day_at(source['end_us']),
            'peaks': dict.fromkeys(e.PEAKS.fields)})
    operation = dict(object=dependencies[1], **{'pass': 1}, at_us=START, operation='get',
        native_rpcs=1, bytes=dependencies[1]['bytes'], elapsed_us=1000)
    load = dict(**TAG, interval=dict(start_us=START, end_us=end),
        segments=stream(segments, 'segments', 'details/segments'),
        operations=stream([operation], 'operations', 'details/operations'), limits=limit_ref, approval=approve(limit_ref))
    pre_segments = []
    for row in sorted(pre_input['serving'], key=lambda v: v['instance']):
        span = row['intervals'][0]
        peaks = dict.fromkeys(e.PEAKS.fields)
        peaks.update(startup_operations_1s=0, close_operations_1s=2, export_operations_1s=2,
            claim_operations_per_second=rat(Fraction(1, 2)), scan_operations_per_second=rat(Fraction(1, 2)),
            publication_operations_per_second=rat(Fraction(1, 2)),
            export_operations_per_second=rat(Fraction(2, 5)), export_bytes_per_second=rat(Fraction(131072, 5)))
        peaks.update(simultaneous_routers=int(row['role'] == 'router'), simultaneous_publishers=0,
            reads_per_second=rat(Fraction(5, 4) if row['role'] == 'router' else 0),
            pending_rows_per_second=rat(Fraction(1001, 4) if row['role'] == 'router' else 0),
            trust_rows_per_second=rat(Fraction(1, 4) if row['role'] == 'router' else 0),
            health_rows_per_second=rat(1 if row['role'] == 'router' else 0),
            configured_routers=int(row['role'] == 'router'), configured_publishers=0,
            configured_reads_per_second=rat(Fraction(5, 4) if row['role'] == 'router' else 0))
        pre_segments.append(dict(day=span['day'], boot=row['instance'], region=row['region'],
            start_us=span['started_at_us'], end_us=span['flushed_at_us'], role=row['role'],
            contender=False, workspace_count=1, peaks=peaks))
    pre_load = dict(**TAG, interval=dict(start_us=pre_input['started_at_us'], end_us=pre_input['flushed_at_us']),
        segments=stream(pre_segments, 'segments', 'details/pre-segments'),
        operations=stream([{**operation, 'at_us': pre_input['started_at_us']}], 'operations', 'details/pre-operations'),
        limits=limit_ref, approval=approve(limit_ref, anchor - 1))
    pre = dict(**TAG, stage='pre', retained=pre_retained,
        output=ref('budgets/pre/output.json', pre_call.output), pre_envelope_sha256=None,
        approval=approve(pre_retained['payload'], anchor - 1), v2_load=pre_load)
    post = dict(**TAG, stage='post', retained=post_retained,
        output=ref('budgets/post/output.json', post_call.output), pre_envelope_sha256=sha256(pre),
        approval=approve(post_retained['payload']), v2_load=load)
    artifacts['proof.pre_budget_sha256'] = e.BUDGET_ENVELOPE.serialize(pre)
    artifacts['proof.post_budget_sha256'] = e.BUDGET_ENVELOPE.serialize(post)

    # Independent source measurements, parsed through the explicit producer seam.
    attachments = []
    shutdown = []
    parsers = {}
    for row in bundle.attachments:
        if row.key.kind == 'shutdown_measurement':
            value = e.SHUTDOWN_MEASUREMENT.parse(row.raw)
            original = {k: v for k, v in value.items() if k != 'source'}
            source = ref('sources/measurement/' + value['day'] + '/' + value['boot'], SHUTDOWN_SOURCE.serialize(original))
            parsers[source['path']] = SHUTDOWN_SOURCE.parse
            value['source'] = source
            row = Row(row.key, e.SHUTDOWN_MEASUREMENT.serialize(value))
            ref(row.key.id, row.raw)
        attachments.append(row)
    for index, row in enumerate(attachments):
        if row.key.kind == 'shutdown_entry':
            value = e.SHUTDOWN_ENTRY.parse(row.raw)
            value['measurement'] = ref(value['measurement']['path'], artifacts[value['measurement']['path']])
            shutdown.append(value)
            attachments[index] = Row(row.key, e.SHUTDOWN_ENTRY.serialize(value))
    all_measurements = ref('measurements/inventory.json', canonical([v['measurement'] for v in shutdown]))
    artifacts['proof.shutdown_inventory_sha256'] = e.SHUTDOWN_INVENTORY.serialize(dict(**TAG,
        writers=stream(shutdown, 'shutdown_writers', 'details/shutdown-real'), measurements=all_measurements,
        approval=approve(all_measurements), complete=True))
    reset_input = dict(rows=[], days=list(bundle.days), proof=dict(resolved_mismatches=[]),
        resets=[], serving=[[b.start_us, b.end_us, b.revision] for b in bundle.inventory.boots if b.role == 'router'],
        manifests={m['day']: {'router_revisions': m['revisions']['router']} for m in
            (a1.MANIFEST.parse(row.raw) for row in bundle.rows if row.key.kind == p1.KINDS[5])})
    if variant in RESET_VARIANTS:
        reset_input['resets'] = [dict(at_us=START + 1, revision=REV, reason=variant)]
    reset_retained, reset_call = delegated('shadow_report.collect_rollbacks', reset_input, [], 'resets/real')
    policy = e.COHORT_SEED_POLICY.parse(artifacts['proof.cohort_seed_policy_sha256'])
    policy['approval'] = approve(policy['configuration'], anchor - 1)
    artifacts['proof.cohort_seed_policy_sha256'] = e.COHORT_SEED_POLICY.serialize(policy)
    history = []
    for reset in json.loads(reset_call.output)['resets']:
        support = stream([reset_retained['payload']], 'files', 'details/reset-support')
        history.append(dict(reset, support=support, resolved_at_us=None, fixed_revision=None, invalidated_at_us=None))
    empty = stream(history, 'reset_history', 'details/reset-history')
    artifacts['proof.resolution_dag_sha256'] = e.RESOLUTION_DAG.serialize(dict(**TAG, retained=reset_retained, history=empty, approvals=[]))

    criteria = {}
    def proof_result(criterion: str, at: int) -> dict[str, Any]:
        criteria[criterion] = ('contract_vectors',)
        # Exact fixture evidence is approved by the test release, not a free-form
        # PASS string. Every check is bound to the criterion and revision wrapper.
        evidence = ref('ci/' + criterion + '/vectors.json', (ROOT / 'shadow_v2_a2/manifest.json').read_bytes())
        return dict(**TAG, criterion=criterion, router_revision=REV, enclave_revision=REV,
            completed_at_us=at, producer='fixture-ci', checks=[dict(name='contract_vectors', result='pass', evidence=evidence)],
            sources=stream([evidence], 'files', 'ci/' + criterion + '/sources'))
    requirements = []
    required = (*EXTERNAL_REQUIRED, 'attestation', 'exposure_acceptance') if pilot_stage else EXTERNAL_REQUIRED
    for criterion in required:
        proof_ref = ref('proofs/' + criterion, e.PROOF_RESULT.serialize(proof_result(criterion, START - 1)))
        requirements.append(dict(name=criterion, stage='shadow-serving', router_revision=REV, enclave_revision=REV,
            evidence=proof_ref, approval=approve(proof_ref)))
    pilot_envelope = None
    if pilot_stage:
        pilot_payload, pilot_files = retained_pilot_fixture(tmp_path, pilot_stage)
        pilot_dependencies = [ref(name, raw) for name, raw in sorted(pilot_files.items())]
        pilot_envelope, _ = delegated('pilot_enablement.' + pilot_stage.replace('-', '_'),
            pilot_payload, pilot_dependencies, 'pilot/' + pilot_stage)
    artifacts['proof.external_proofs_sha256'] = e.EXTERNAL_PROOFS.serialize(dict(**TAG, requirements=requirements, pilot=pilot_envelope))
    ledgers = []
    previous = None
    for raw in bundle.ledgers:
        ledger = a1.PREREQUISITE_LEDGER.parse(raw)
        for field in ('policy_sha256', 'config_sha256', 'fixture_sha256'):
            material = artifacts['config/probe.json'] if field != 'fixture_sha256' else (ROOT / 'shadow_v2_a2/manifest.json').read_bytes()
            ledger[field] = ref('ledger/' + ledger['boot'] + '/' + field, material)['sha256']
        for field in ('transport', 'size', 'regional_load', 'boot_transport', 'observer_freshness', 'marker_keys'):
            value = proof_result(field, ledger['boot_at_us'])
            from trusted_router.async_settle_shadow_v2_gate_inputs import PREREQUISITE_CONTEXT
            context = {k: ledger[k] for k in ('role', 'boot', 'revision', 'region', 'boot_at_us')}
            context.update(criterion=field, completed_at_us=value['completed_at_us'])
            context_ref = ref('ledger/' + ledger['boot'] + '/' + field + '/context', PREREQUISITE_CONTEXT.serialize(context))
            check_ref = value['checks'][0]['evidence']
            value['sources'] = stream(sorted([check_ref, context_ref], key=lambda v: v['path']), 'files', 'ledger/' + ledger['boot'] + '/' + field + '/sources')
            material = e.PROOF_RESULT.serialize(value)
            item = ref('ledger/' + ledger['boot'] + '/' + field, material)
            ledger[field] = dict(sha256=item['sha256'], completed_at_us=value['completed_at_us'])
        ledger['previous_ledger_sha256'] = previous
        raw = a1.PREREQUISITE_LEDGER.serialize(ledger)
        ledgers.append(raw)
        previous = r.file_digest(raw)
    bundle = replace(bundle, ledgers=tuple(ledgers), attachments=tuple(attachments))
    # All exact verifier files are named release dependencies, including ordered keys.
    verifier_pins = {}
    for index, approved in enumerate(tool_ports.approvals):
        for name, raw in (('source', approved.source), ('fixture', approved.fixture), ('keys', approved.keyset)):
            ref('release/' + str(index) + '/' + name, raw)
        verifier_pins[approved.entry_point] = (r.file_digest(approved.source), r.file_digest(approved.fixture),
                                               r.file_digest(approved.keyset) if approved.keys else None)
    signature_material = []
    for signature in bundle.signatures:
        value = asdict(signature)
        value.pop('keys')
        value['verifier'] = 'binding'
        signature_material.append(value)
    ref('loading/signatures.json', canonical(signature_material))
    # The real delegated/shutdown streams replace seal()'s construction-only
    # pages; retaining those obsolete pages would leave stale FILE_REFs.
    artifacts = {path: raw for path, raw in artifacts.items()
        if not path.startswith(('details/budget-dependencies/', 'details/shutdown/'))}
    trusted_pins = {path: r.file_digest(raw) for path, raw in artifacts.items() if not path.startswith('proof.')}
    plan_raw = canonical(dict(artifacts=trusted_pins, criteria=criteria, verifiers=verifier_pins, required=list(required)))
    plan_path = 'loading/release-plan.json'
    ref(plan_path, plan_raw)
    release = ReleasePlan(plan_path, plan_raw, trusted_pins, criteria, verifier_pins, required)
    tool_ports = replace(tool_ports, release=release, measurements=parsers)
    bundle = commit_originals(bundle, artifacts)
    bundle = replace(bundle, budget=replace(bundle.budget, pre_completed_us=anchor - 1))
    expected = dict(pre_input=pre_input, pre_output=pre_output, post_input=post_input,
        post_output=json.loads(post_call.output), reset_output=json.loads(reset_call.output),
        segments=segments, projected_segments=pre_segments, measurements=measured, rates=[asdict(rate) for rate in bundle.budget.rates])
    if chain:
        bundle, tool_ports = attach_prior_chain(bundle, tool_ports)
    return bundle, tool_ports, expected


def retained_pilot_fixture(tmp_path: Path, stage: str) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Real retained pilot inputs and captures, with finite relative file paths."""
    from unittest.mock import patch

    from scripts.async_settle import pilot_enablement as pilot
    from tests import test_async_settle_pilot_tools as fixtures
    directory = tmp_path / 'pilot-originals'
    directory.mkdir(exist_ok=True)
    original_artifact = fixtures.artifact
    def artifact(path: Path, name: str, value: Any) -> dict[str, Any]:
        result = original_artifact(path, name, value)
        return {**result, 'path': 'pilot-originals/' + name}
    with patch.object(fixtures, 'artifact', artifact), patch.object(pilot, 'Path', lambda path: tmp_path / path):
        data = fixtures.fleet_bundle(directory)
        if stage == 'pre-flip':
            source = artifact(directory, 'export.json', [])
            selected = fixtures.select([fixtures.trial()])
            selected['exports'] = {source['path']: source['sha256']}
            data.update(drain=artifact(directory, 'drain.json', selected),
                ci={name: artifact(directory, name, {'passed': True}) for name in ('F1', 'F2b', 'F2c')},
                signer=dict(epoch=1, audience='router-settlement', kid='kid', issuer='issuer', mounted=True,
                    public_key=fixtures.FIXTURE['public_key'], verification=artifact(directory, 'signer.json', {'passed': True})))
            data['serving'][0]['pins'].update(TR_ASYNC_SETTLE_TICKET_KID='kid', TR_ASYNC_SETTLE_TICKET_ISSUER='issuer',
                TR_ASYNC_SETTLE_TICKET_AUDIENCE='router-settlement', TR_ASYNC_SETTLE_AUTHORITY_EPOCH='1',
                TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE='/mounted/key.pem')
        else:
            assert stage == 'post-flip'
            data['serving'][0]['pins'].update(TR_ASYNC_SETTLE_ENABLED='true', TR_ASYNC_SETTLE_PROTECTION='true',
                TR_ASYNC_SETTLE_PILOT_WORKSPACES=pilot.PILOT, TR_ASYNC_SETTLE_PILOT_CAP_MICRO='5000000')
            data['serving'][1]['pins'].update(TR_ASYNC_SETTLE_NEGOTIATE='on', TR_ASYNC_SETTLE_SHADOW='off')
            data.update(rollback=artifact(directory, 'rollback.json', dict(status='PASS', states=[
                'admission_on', 'pending', 'admission_off', 'drained', 'protection_off'])),
                outbox=[dict(authorization_id='a', intent_kind='settle', workspace_id=pilot.PILOT,
                    status='done', created_at=100, terminal_at=102, async_version=1)],
                handoff={'a.settle': dict(region='us-central1', handoff_us=3000)}, observed_peak_concurrency=4)
    return data, {'pilot-originals/' + path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


# Semantic variants change one source fact before the same generator derives
# dependencies, retained outputs, approvals, release authority and root graph.
BUDGET_VARIANTS = ('simultaneous_capacity_gap', 'freshness_budget_gap', 'evidence_export_budget_gap')
RESET_VARIANTS = ('hash', 'identity', 'normalization', 'evaluator_disagreement', 'counter_mismatch',
    'reviewed_resolution', 'revision_rollback', 'exclusion_conflict')
HEALTH_VARIANTS = ('health_source_gap', 'health_timestamp_gap', 'health_elapsed_gap')
SEMANTIC_VARIANTS = (*BUDGET_VARIANTS, *RESET_VARIANTS, *HEALTH_VARIANTS)



@lru_cache(maxsize=1)
def _transport_object(raw: bytes) -> dict[str, Any]:
    return json.loads(raw)


def transport_object(raw: bytes) -> dict[str, Any]:
    return copy.deepcopy(_transport_object(raw))


def mutate_transport(raw: bytes, gap: str) -> bytes:
    """One original source mutation; never edits a decoded typed gate view."""
    # Original strings are immutable; only the outer lists and one selected
    # source are replaced. Shared transport parsing is keyed by exact bytes.
    original_value = transport_object(raw)
    value = {**original_value, **{name: list(original_value[name])
        for name in ('rows', 'attachments', 'preimages', 'ledgers')}}
    def row(kind: str, change: Any = None, *, remove: bool = False, conflict: bool = False, occurrence: int = 0) -> None:
        collection = 'rows' if any(r['kind'] == kind for r in value['rows']) else 'attachments'
        rows = value[collection]
        index = [i for i, r in enumerate(rows) if r['kind'] == kind][occurrence]
        if remove:
            rows.pop(index)
            return
        original = rows[index]
        body = json.loads(original['raw'])
        change(body)
        changed = {**original, 'raw': canonical(body).decode()}
        if conflict:
            rows.append(changed)
        else:
            rows[index] = changed
    def absent(path: str) -> None:
        index = next(i for i, p in enumerate(value['preimages']) if p['name'] == path)
        value['preimages'].pop(index)
    counters = {
        'policy_version_gap': lambda c: c.update(policy_version='shadow-v1'),
        'evidence_schema_gap': lambda c: c.update(unrecognized=1),
        'legacy_region_label': lambda c: c.update(region='wrong'),
        'phase_coverage_gap': lambda c: c['buckets'][1]['dimensions'].update(adapter='anthropic'),
        'observer_cadence_gap': lambda c: c['admission_observer'].update(workspace_reads=0),
        'observer_budget_gap': lambda c: c['admission_observer'].update(max_consecutive_failures=3),
        'publisher_accounting_gap': lambda c: c['publisher'].update(claims=0),
        'checkpoint_reconciliation_gap': lambda c: c.update(durable_event_hwm=0),
        'evidence_loss_gap': lambda c: c.update(loss_count=1),
        'writer_closure_gap': lambda c: c.update(closed=False),
    }
    receipts = {
        'authorize_coverage_gap': lambda p: (p.update(first_seq=p['last_seq']), p['events'].pop(0)),
        'authorization_link_gap': lambda p: p['events'][0].update(facts=None),
        'authorization_terminal_conflict': lambda p: p['events'][1]['identity'].update(epoch=2),
        'authorization_horizon_gap': lambda p: p['events'][1].update(receipt_at_us=p['events'][1]['event_at_us'] + 172802000000),
        'observed_partition_gap': lambda p: p['events'][1].update(partition='C', comparison='unevaluable'),
        'exclusion_proof_gap': lambda p: p['events'][1].update(partition='B', eligibility='excluded', proof_kind='observed_binding', prediction=None, comparison='unevaluable'),
        'prediction_demand_gap': lambda p: p['events'][1]['prediction'].update(prediction='unknown'),
        'prediction_unknown_gap': lambda p: p['events'][1]['prediction'].update(prediction='unknown'),
        'health_slot_gap': lambda p: p['events'][1]['prediction'].update(health_slot=0),
        'retry_original_gap': lambda p: p['events'][1].update(retry_original=dict(day=p['day'], boot='absent', event_seq=f'{1:020}')),
    }
    removals = {'writer_roster_gap': p1.KINDS[3], 'terminal_coverage_gap': p1.KINDS[2],
        'eligible_coverage_gap': p1.KINDS[0], 'authorization_obligation_gap': 'authorization_obligation',
        'shadow_health_gap': 'health_basis', 'publisher_trace_gap': 'publisher_trace',
        'close_result_identity_gap': a1.KINDS[-1], 'handoff_coverage_gap': 'handoff_accounting',
        'close_handoff_budget_gap': 'shutdown_measurement'}
    files = {'artifact_gap': 'proof.external_proofs_sha256', 'external_proofs_missing': 'proof.external_proofs_sha256',
        'pilot_prerequisite_gap': 'proof.external_proofs_sha256', 'missing_support': 'proof.resolution_dag_sha256',
        'pre_enable_budget': 'proof.pre_budget_sha256', 'fleet_headroom_gap': 'loading/budget-measurements.json'}
    if gap in counters:
        row(p1.KINDS[3], counters[gap])
    elif gap in receipts:
        row(p1.KINDS[2], receipts[gap])
    elif gap in removals:
        row(removals[gap], remove=True)
    elif gap in files:
        absent(files[gap])
    elif gap == 'regional_roster_unknown':
        proof = json.loads(value['proof'])
        proof['instance_regions_by_day'] = {}
        value['proof'] = canonical(proof).decode()
    elif gap == 'legacy_sample_region':
        row(p1.KINDS[0], lambda s: s['deployment'].update(region='wrong'))
    elif gap == 'flag_interval_gap':
        row(p1.KINDS[5], lambda m: m.update(disabled_flag_intervals=[]))
    elif gap == 'boot_prerequisite_gap':
        value['ledgers'].pop(0)
    elif gap == 'publisher_budget_gap':
        index = next(i for i, p in enumerate(value['preimages']) if p['name'] == 'proof.cohort_seed_policy_sha256')
        source = value['preimages'][index]
        policy = json.loads(source['raw'])
        policy['maximum_publishers'] = 0
        value['preimages'][index] = {**source, 'raw': canonical(policy).decode()}
    elif gap == 'close_result_conflict':
        row(a1.KINDS[-1], lambda v: v.update(elapsed_us=v['elapsed_us'] + 1), conflict=True)
    elif gap in ('positive_seed_gap', 'regional_seed_gap'):
        gate = json.loads(value['transport'])
        sample = json.loads(next(r['raw'] for r in value['rows'] if r['kind'] == p1.KINDS[0]))
        gate['completed_at_us'] = sample['observed_at_us']
        value['transport'] = canonical(gate).decode()
    elif gap == 'continuous_window_day_gap':
        def interrupted(manifest: dict[str, Any]) -> None:
            interval = manifest['disabled_flag_intervals'][0]['interval']
            manifest.update(complete=False, gaps=[dict(interval=interval, reason='writer_closure_gap')])
        row(p1.KINDS[5], interrupted, occurrence=2)
    elif gap == 'continuous_window_gap':
        value['completed_at_us'] -= 3000000
    else:
        raise ValueError("semantic variant required: " + gap)
    changed_keys = [key for key in value if value[key] != original_value[key]]
    assert len(changed_keys) == 1
    key = changed_keys[0]
    marker = canonical(key) + b':'
    start = raw.index(marker) + len(marker)
    before, after = original_value[key], value[key]
    if isinstance(before, list):
        if len(after) == len(before) + 1:
            # Conflict witnesses append one new original candidate.
            assert all(a is b for a, b in zip(before, after, strict=False))
            last = canonical(before[-1])
            end = raw.index(last, start) + len(last)
            assert raw[end:end + 1] == b']'
            return raw[:end] + b',' + canonical(after[-1]) + raw[end:]
        index = next(i for i, item in enumerate(before) if i >= len(after) or item is not after[i])
        fragment = canonical(before[index])
        start = raw.index(fragment, start)
        end = start + len(fragment)
        if len(after) == len(before) - 1:
            if raw[end:end + 1] == b',':
                end += 1
            elif raw[start - 1:start] == b',':
                start -= 1
            replacement = b''
        else:
            assert len(after) == len(before)
            replacement = canonical(after[index])
    else:
        fragment = canonical(before)
        assert raw[start:start + len(fragment)] == fragment
        end = start + len(fragment)
        replacement = canonical(after)
    return raw[:start] + replacement + raw[end:]


def _build_original_prior_chain() -> tuple[Bundle, Any, dict[str, bytes]]:
    """One real loader/evaluator/renderer execution per warm-up stage/session."""
    import tempfile

    START = 1791244800000000
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_result import render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    path = Path(tempfile.mkdtemp(prefix='p5-prior-chain-'))
    original, ports, _ = real_fixture(path, seconds=20, start_us=START - 20000000, chain=False, traffic=False)
    # The final CLOSE_RESULT acknowledgment lands one second after the
    # warm-up interval. Evaluation completion follows that original evidence.
    original = replace(original, transport=None, pre=None, fleet=None, serving=None, completed_at_us=START + 1000000)
    archived: dict[str, bytes] = {}
    for name, stage in (('transport', 'transport'), ('pre', 'fleet-budgets-pre'), ('fleet', 'fleet-budgets')):
        original = replace(original, preimages=(*original.preimages, *(Preimage(k, v) for k, v in archived.items())))
        loaded = load(transport(original), ports)
        result = evaluate(loaded, ports, gate=stage)
        assert result.verdict == 'PASS', (stage, [(f.gap, f.detail) for f in result.failures])
        output, attachments = render(result)
        # Every input and output remains original bytes, keyed by content.
        for raw in (output, *attachments, *(p.raw for p in original.preimages),
                    *(row.raw for row in (*original.rows, *original.attachments)),
                    *original.ledgers, original.proof):
            assert raw is not None
            archived['prior-artifacts/' + r.file_digest(raw)] = raw
        original = replace(original, **{name: output})
    return original, ports, archived


def attach_prior_chain(bundle: Bundle, ports: Any) -> tuple[Bundle, Any]:
    prior, prior_ports, archive = original_prior_chain()
    assert ports.release and prior_ports.release
    artifacts = {p.name: p.raw for p in bundle.preimages}
    artifacts.update(archive)
    plan = ports.release
    approved = dict(plan.artifacts)
    prior_plan = prior_ports.release.raw
    path = 'prior-artifacts/' + r.file_digest(prior_plan)
    approved[path] = r.file_digest(prior_plan)
    raw_plan = canonical(dict(artifacts=approved, criteria=plan.criteria, verifiers=plan.verifiers, required=list(plan.required)))
    artifacts[plan.path] = raw_plan
    ports = replace(ports, release=replace(plan, artifacts=approved, raw=raw_plan))
    return replace(bundle, transport=prior.transport, pre=prior.pre, fleet=prior.fleet, serving=None,
        preimages=tuple(Preimage(k, v) for k, v in artifacts.items())), ports


def rerender_prior(bundle: Bundle, name: str, completed_at_us: int) -> Bundle:
    """Change a prior evaluation time through the real pipeline, not its envelope."""
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    from trusted_router.async_settle_shadow_v2_gate_prior import STAGES
    from trusted_router.async_settle_shadow_v2_gate_result import render
    from trusted_router.async_settle_shadow_v2_gates import evaluate
    original, ports, _ = original_prior_chain()
    original = replace(original, completed_at_us=completed_at_us)
    result = evaluate(load(transport(original), ports), ports, gate=STAGES[name])
    assert result.verdict == 'PASS', result.gaps
    output, attachments = render(result)
    archive = {p.name: p.raw for p in bundle.preimages}
    for raw in (output, *attachments):
        archive['prior-artifacts/' + r.file_digest(raw)] = raw
    return replace(bundle, **{name: output}, preimages=tuple(Preimage(k, v) for k, v in archive.items()))


def alter_prior_inventory(bundle: Bundle, name: str, select: Any, change: Any) -> Bundle:
    """Mutate one original prior input and recommit only its inventory ancestors.

    Deliberately do not repair the evidence's semantic/preimage commitments.
    This makes the outer report contract valid, isolating graph verification.
    """
    files = {p.name: p.raw for p in bundle.preimages}
    by_hash = {r.file_digest(raw): raw for raw in files.values()}
    envelope = a1.GATE_OUTPUT.parse(getattr(bundle, name))
    report = e.GATE_REPORT.parse(by_hash[envelope['coverage_sha256']])
    entries = prior_inventory_entries(report, by_hash)
    changed = 0
    for entry in entries:
        leaf = e.INPUT_LEAF.parse(by_hash[entry['bundle']['sha256']])
        for ref in leaf['keys']:
            if not select(ref['path']):
                continue
            raw = change(by_hash[ref['sha256']])
            changed += 1
            previous_digest = ref['sha256']
            ref.update(sha256=r.file_digest(raw), bytes=len(raw))
            if ref['path'].startswith('rows/') and ref['path'].endswith('/' + previous_digest):
                ref['path'] = ref['path'].rsplit('/', 1)[0] + '/' + ref['sha256']
            files['prior-artifacts/' + r.file_digest(raw)] = raw
            if ref['path'] == 'proof_manifest':
                report['proof_manifest_sha256'] = r.file_digest(raw)
                report['prerequisite_ledger_root'] = a1.PROOF_MANIFEST.parse(raw)['ledger_root']
        leaf['keys'].sort(key=lambda ref: ref['path'])
        entry.update(first_key=leaf['keys'][0]['path'], last_key=leaf['keys'][-1]['path'])
        raw = e.INPUT_LEAF.serialize(leaf)
        entry['bundle'].update(sha256=r.file_digest(raw), bytes=len(raw))
        files['prior-artifacts/' + r.file_digest(raw)] = raw
    assert changed == 1, changed
    report['input_inventory'], pages = detail(entries, 'input_inventory')
    for page in pages:
        raw = canonical(page)
        files['prior-artifacts/' + r.file_digest(raw)] = raw
    raw = e.GATE_REPORT.serialize(report)
    files['prior-artifacts/' + r.file_digest(raw)] = raw
    envelope.update(coverage_sha256=sha256(report), proof_manifest_sha256=report['proof_manifest_sha256'],
        prerequisite_ledger_root=report['prerequisite_ledger_root'])
    return replace(bundle, **{name: a1.GATE_OUTPUT.serialize(envelope)},
        preimages=tuple(Preimage(k, v) for k, v in files.items()))


def prior_inventory_entries(report: Any, by_hash: Any) -> Any:
    pages = []
    tail = report['input_inventory']['tail_sha256']
    while tail is not None:
        page = r.STREAMS['input_inventory'][1].parse(by_hash[tail])
        pages.append(page)
        tail = page['previous_sha256']
    pages.reverse()
    r.validate_detail(report['input_inventory'], pages, stream='input_inventory')
    return [entry for page in pages for entry in page['entries']]


def original_prior_chain() -> tuple[Bundle, Any, dict[str, bytes]]:
    """Content-keyed across xdist workers; only the lock owner evaluates stages."""
    source_root = Path(__file__).parents[1]
    sources = sorted((source_root / 'src/trusted_router').glob('async_settle_shadow_v2*.py'))
    sources += [Path(__file__), source_root / 'tests/shadow_v2_gate_fixture_generator.py',
                source_root / 'scripts/async_settle/shadow_report.py', source_root / 'scripts/async_settle/pilot_enablement.py']
    key = r.file_digest(b''.join(path.read_bytes() for path in sources))
    value = _shared_prior_chain(key)
    # Bound methods close over immutable frozen contract schemas (mappingproxy
    # fields); copy all caller-owned containers, retain callable identities.
    memo = {id(parser): parser for parser in value[1].measurements.values()}
    return copy.deepcopy(value, memo)


@cache
def _shared_prior_chain(key: str) -> tuple[Bundle, Any, dict[str, bytes]]:
    import fcntl
    import tempfile

    from scripts.async_settle import shadow_report
    from scripts.async_settle.async_settle_shadow_v2_gate_tools import read_release_plan
    from tests.shadow_v2_gate_fixture_generator import base_ports
    from trusted_router.async_settle_shadow_v2_gate_loader import load
    path = Path(tempfile.gettempdir()) / ('p5-chain-' + key + '.json')
    with path.with_suffix('.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not path.exists():
            original, ports, archive = _build_original_prior_chain()
            path.write_bytes(canonical(dict(bundle=transport(original).decode(),
                release=ports.release.raw.decode(), archive={k: v.decode() for k, v in archive.items()})))
            return original, ports, archive
        data = json.loads(path.read_bytes())
    plan_raw = data['release'].encode()
    release = read_release_plan(plan_raw, r.file_digest(plan_raw))
    base = base_ports()
    ports = shadow_report.shadow_v2_ports(base.approvals[0].keyset, base.approvals[1].keyset, release=release)
    return load(data['bundle'].encode(), ports), ports, {k: v.encode() for k, v in data['archive'].items()}
