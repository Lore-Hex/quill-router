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
    ctx.rows = {row.key: a1.validate_row(row.key.kind, row.key.id, row.raw) for row in bundle.rows}
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


def transport(bundle: Bundle) -> bytes:
    """Only original bytes cross the production loading boundary."""
    def rows(values: tuple[Row, ...]) -> list[dict[str, Any]]:
        return [dict(kind=r.key.kind, id=r.key.id, raw=r.raw.decode()) for r in values]
    return canonical(dict(policy_version='shadow-v2', contract_revision='a1',
        rows=rows(bundle.rows), attachments=rows(bundle.attachments), proof=bundle.proof.decode() if bundle.proof else None,
        ledgers=[raw.decode() for raw in bundle.ledgers], days=list(bundle.days), completed_at_us=bundle.completed_at_us,
        preimages=[dict(name=p.name, raw=p.raw.decode()) for p in bundle.preimages],
        **{key: getattr(bundle, key).decode() if getattr(bundle, key) else None for key in ('transport', 'fleet', 'pre', 'serving')}))


def reseal(bundle: Bundle, artifacts: dict[str, bytes]) -> Bundle:
    """Reconstruct all F/C/R roots after changing original fixture evidence."""
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
    ctx.rows = {row.key: a1.validate_row(row.key.kind, row.key.id, row.raw) for row in bundle.rows}
    ctx.attachments = {row.key: ATTACHMENTS[row.key.kind].parse(row.raw) for row in bundle.attachments}
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
            (manifest if owner == 'manifest' else proof)[key] = r.entry_root(
                [r._root_entry(x['key'], x['bodies']) for x in inputs[field]], stream=stream)
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


def real_fixture(tmp_path: Path) -> tuple[Bundle, Any, dict[str, Any]]:
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
        ports,
        pre_enable_bundle,
    )
    from trusted_router.async_settle_shadow_evidence import Counters
    from trusted_router.async_settle_shadow_v2_gate_ports import ReleasePlan
    from trusted_router.async_settle_shadow_v2_gate_views import (
        ALLOWANCES,
        BUDGET_MEASUREMENTS,
        COMPONENTS,
        EXTERNAL_REQUIRED,
        MEASUREMENTS_PATH,
        SHUTDOWN_SOURCE,
    )

    bundle = build(tmp_path, churn=False)
    assert bundle.inventory and bundle.budget
    end = bundle.completed_at_us
    artifacts = {p.name: p.raw for p in bundle.preimages}
    def ref(path: str, raw: bytes) -> dict[str, Any]:
        artifacts[path] = raw
        return dict(path=path, sha256=r.file_digest(raw), bytes=len(raw))
    def approve(reference: dict[str, Any], at: int = end) -> dict[str, Any]:
        record = dict(**TAG, subject_sha256=reference['sha256'], reviewer='fixture-reviewer', reviewed_at_us=at, decision='approved')
        return {k: v for k, v in record.items() if k not in TAG} | dict(record=ref('reviews/' + reference['sha256'] + '/' + str(at), e.REVIEW_RECORD.serialize(record)))
    def stream(values: list[dict[str, Any]], name: str, path: str) -> dict[str, Any]:
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
    pre_input.update(started_at_us=START - 60000001, flushed_at_us=START - 1)
    for row in pre_input['serving']:
        row['intervals'] = [dict(day=day_at(START - 1), started_at_us=START - 60000001, flushed_at_us=START - 1)]
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
    completed_pre = ref('budgets/pre-completed.json', canonical(pre_output | dict(completed_at_us=START - 1)))
    serving = []
    for path, raw in artifacts.copy().items():
        if path.endswith('/serving.json'):
            serving.extend(json.loads(raw))
    counter = Counters(REGION, REV, clock=lambda: START / 1000000)
    counter._day()
    _, legacy_body = counter.snapshot(closed=True)[0]
    legacy_boot = legacy_body['instance']
    legacy_serving = [{**row, 'instance': legacy_boot if row['role'] == 'router' else row['instance']} for row in serving]
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
    from trusted_router.async_settle_shadow_v2_gate_views import TOTALS
    measured.update({k: getattr(bundle.budget, k) for k in TOTALS})
    measured['component_us'] = {k: int(bundle.budget.component_seconds[k] * 1000000) for k in COMPONENTS}
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
    limit_ref = ref('budgets/limits.json', canonical(limits))
    segments = []
    for boot in sorted(bundle.inventory.boots, key=lambda b: b.boot):
        peaks = dict.fromkeys(e.PEAKS.fields)
        peaks.update(simultaneous_routers=int(boot.role == 'router'), simultaneous_publishers=int(boot.contender),
            reads_per_second=rat(Fraction(13, 4) if boot.role == 'router' else 0),
            pending_rows_per_second=rat(Fraction(1001, 4) if boot.role == 'router' else 0),
            claim_operations_per_second=rat(Fraction(1, 2) if boot.role == 'router' else 0),
            scan_operations_per_second=rat(Fraction(1, 2) if boot.role == 'router' else 0),
            publication_operations_per_second=rat(Fraction(1, 2) if boot.role == 'router' else 0),
            trust_rows_per_second=rat(Fraction(1, 4) if boot.role == 'router' else 0),
            health_rows_per_second=rat(1 if boot.role == 'router' else 0))
        segments.append(dict(day=day_at(boot.start_us), boot=boot.boot, region=boot.region,
            start_us=boot.start_us, end_us=boot.end_us, role=boot.role, contender=boot.contender, workspace_count=1, peaks=peaks))
    operation = dict(object=dependencies[1], **{'pass': 1}, at_us=START, operation='get',
        native_rpcs=1, bytes=dependencies[1]['bytes'], elapsed_us=1000)
    load = dict(**TAG, interval=dict(start_us=START, end_us=end),
        segments=stream(segments, 'segments', 'details/segments'),
        operations=stream([operation], 'operations', 'details/operations'), limits=limit_ref, approval=approve(limit_ref))
    pre_segments = []
    for row in sorted(pre_input['serving'], key=lambda v: v['instance']):
        span = row['intervals'][0]
        peaks = dict.fromkeys(e.PEAKS.fields)
        peaks.update(configured_routers=int(row['role'] == 'router'), configured_publishers=0,
            configured_reads_per_second=rat(Fraction(5, 4) if row['role'] == 'router' else 0))
        pre_segments.append(dict(day=span['day'], boot=row['instance'], region=row['region'],
            start_us=span['started_at_us'], end_us=span['flushed_at_us'], role=row['role'],
            contender=False, workspace_count=1, peaks=peaks))
    pre_load = dict(**TAG, interval=dict(start_us=pre_input['started_at_us'], end_us=pre_input['flushed_at_us']),
        segments=stream(pre_segments, 'segments', 'details/pre-segments'),
        operations=stream([{**operation, 'at_us': pre_input['started_at_us']}], 'operations', 'details/pre-operations'),
        limits=limit_ref, approval=approve(limit_ref, START - 1))
    pre = dict(**TAG, stage='pre', retained=pre_retained,
        output=ref('budgets/pre/output.json', pre_call.output), pre_envelope_sha256=None,
        approval=approve(pre_retained['payload'], START - 1), v2_load=pre_load)
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
    reset_retained, reset_call = delegated('shadow_report.collect_rollbacks', reset_input, [], 'resets/real')
    empty = stream([], 'reset_history', 'details/reset-history')
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
    for criterion in EXTERNAL_REQUIRED:
        proof_ref = ref('proofs/' + criterion, e.PROOF_RESULT.serialize(proof_result(criterion, START - 1)))
        requirements.append(dict(name=criterion, stage='shadow-serving', router_revision=REV, enclave_revision=REV,
            evidence=proof_ref, approval=approve(proof_ref)))
    artifacts['proof.external_proofs_sha256'] = e.EXTERNAL_PROOFS.serialize(dict(**TAG, requirements=requirements, pilot=None))
    ledgers = []
    previous = None
    for raw in bundle.ledgers:
        ledger = a1.PREREQUISITE_LEDGER.parse(raw)
        for field in ('policy_sha256', 'config_sha256', 'fixture_sha256'):
            material = artifacts['config/probe.json'] if field != 'fixture_sha256' else (ROOT / 'shadow_v2_a2/manifest.json').read_bytes()
            ledger[field] = ref('ledger/' + ledger['boot'] + '/' + field, material)['sha256']
        for field in ('transport', 'size', 'regional_load', 'boot_transport', 'observer_freshness', 'marker_keys'):
            value = proof_result(field, ledger['boot_at_us'])
            from trusted_router.async_settle_shadow_v2_gate_provenance import PREREQUISITE_CONTEXT
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
    trusted_pins = {path: r.file_digest(raw) for path, raw in artifacts.items() if not path.startswith('proof.')}
    plan_raw = canonical(dict(artifacts=trusted_pins, criteria=criteria, verifiers=verifier_pins, required=list(EXTERNAL_REQUIRED)))
    plan_path = 'loading/release-plan.json'
    ref(plan_path, plan_raw)
    release = ReleasePlan(plan_path, plan_raw, trusted_pins, criteria, verifier_pins, EXTERNAL_REQUIRED)
    tool_ports = replace(tool_ports, release=release, measurements=parsers)
    bundle = reseal(bundle, artifacts)
    expected = dict(pre_input=pre_input, pre_output=pre_output, post_input=post_input,
        post_output=json.loads(post_call.output), reset_output=json.loads(reset_call.output),
        measurements=measured, rates=[asdict(rate) for rate in bundle.budget.rates])
    return bundle, tool_ports, expected
