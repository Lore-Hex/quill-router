"""Round 1: real P3/P4 writers meet P5; no gate evidence fixture generator.

The test owns the simulated deployment and approves only facts it knows.
Missing production collectors stay missing, with an exact BLOCKED baseline.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.datastructures import Headers

from scripts.async_settle.shadow_report import shadow_v2_ports
from tests.test_async_settle_shadow import signer
from tests.test_async_settle_shadow_observation import fake_store
from tests.test_async_settle_ticket import authorization, endpoint, runtime
from trusted_router import billing_snapshot as billing
from trusted_router.async_settle_shadow_binding import verify_binding
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_journal import (
    CLOSE_RESULT_KIND,
    CLOSURE_KIND,
    COUNTER_KIND,
    PAGE_KIND,
    SAMPLE_KIND,
)
from trusted_router.async_settle_shadow_projection import project
from trusted_router.async_settle_shadow_v2_a1_evidence import PROOF_MANIFEST
from trusted_router.async_settle_shadow_v2_a2_evidence import (
    COHORT_SEED_POLICY,
    INDEPENDENT_INVENTORY,
    REVIEW_RECORD,
)
from trusted_router.async_settle_shadow_v2_contract import COMPARED_FULL
from trusted_router.async_settle_shadow_v2_export import (
    DAY_KINDS,
    MemoryPort,
    NativePort,
    OriginalObject,
    OriginalRow,
    digest,
    export_day,
)
from trusted_router.async_settle_shadow_v2_gate_loader import load
from trusted_router.async_settle_shadow_v2_gate_release import decode_release_plan
from trusted_router.async_settle_shadow_v2_gate_result import render
from trusted_router.async_settle_shadow_v2_gates import evaluate
from trusted_router.config import Settings
from trusted_router.detached_jws import b64encode, canonical
from trusted_router.schemas import GatewayAuthorizeRequest, GatewaySettleRequest
from trusted_router.services.async_settle import Admission
from trusted_router.services.async_settle_shadow_cohort_runtime import Runtime
from trusted_router.services.async_settle_shadow_health import Service

DAY = '2026-10-12'
START = 1791763200000000
REVISION = 'a' * 40
TAG = dict(v=2, policy_version='shadow-v2', contract_revision='a2')


def audit_artifact(name, raw):
    directory = os.environ.get('V2_LIVE_AUDIT_DIR')
    if directory:
        Path(directory).mkdir(parents=True, exist_ok=True)
        (Path(directory) / name).write_bytes(raw)


class ObjectSink:
    """Actual immutable attachment PUT/GET target, including fake generations."""
    def __init__(self):
        self.objects = {}
        self.calls = []

    def put(self, key, body, timeout, *, deadline, clock):
        assert 0 < timeout <= 2 and clock() < deadline
        assert key not in self.objects
        self.calls.append(('put', key, len(body)))
        self.objects[key] = OriginalObject(key, body, str(len(self.objects) + 1))

    def get(self, key, timeout, *, deadline, clock):
        assert clock() < deadline
        self.calls.append(('get', key, 0))
        obj = self.objects.get(key)
        return obj.body if obj else None


def request(header, value):
    return SimpleNamespace(headers=Headers({header: value}))


def operator_collector_standins(boot, start, end, region, signatures, db, shutdown_wait_us):
    """Only this test's known roster/config and real tool release bytes.

    Uncollected budgets, shutdown metrics, readiness and external CI cannot be
    asserted by this simulated writer. Their commitments are explicitly absent.
    """
    files = {}

    def file(path, raw):
        files[path] = raw
        return dict(path=path, sha256=digest(raw), bytes=len(raw))

    def approval(path, ref):
        record = dict(**TAG, subject_sha256=ref['sha256'], reviewer='test-operator',
            reviewed_at_us=end, decision='approved')
        record_ref = file(path, REVIEW_RECORD.serialize(record))
        return {k: record[k] for k in ('subject_sha256', 'reviewer', 'reviewed_at_us', 'decision')} | {'record': record_ref}

    roster = [dict(instance=boot, role='router', revision=REVISION, region=region,
        intervals=[dict(day=DAY, started_at_us=start, flushed_at_us=end)],
        pins=dict(TR_ASYNC_SETTLE_ENABLED='false', TR_ASYNC_SETTLE_PROTECTION='false',
                  TR_SETTLE_OUTBOX_FAST_DRAIN_ENABLED='false'))]
    serving = file('collector/serving.json', canonical(roster))
    deployment = file('collector/deployment.json', canonical(dict(serving=roster)))
    inventory = dict(**TAG, day=DAY, serving=serving, deployment=deployment,
        approval=approval('reviews/deployment.json', deployment), complete=True)
    file(DAY + '/manifest.independent_inventory_sha256', INDEPENDENT_INVENTORY.serialize(inventory))
    config = file('operator/cohort.json', canonical(dict(workspace_count=1, eligible_only=True,
        supported_candidates=1, fallbacks=False, routes=['chat.completions', 'responses'])))
    file('manifest.config_sha256', files[config['path']])
    file('manifest.fixture_sha256', Path('tests/fixtures/async_settlement/shadow_v2_a1/manifest.json').read_bytes())
    cohort = dict(**TAG, workspace_fingerprints=[digest(b'ws-v1')], regional_seed_policy='each_traffic_region',
        required_regions=[region], primary_region=region, maximum_publishers=1, configuration=config,
        approval=approval('reviews/cohort.json', config))
    file('proof.cohort_seed_policy_sha256', COHORT_SEED_POLICY.serialize(cohort))
    # Ephemeral response-capture stand-in; never recover a token from a sample.
    file('loading/signatures.json', canonical(signatures))
    # Independent test scheduler measured this shutdown. No physical latency,
    # native RPC cost or production grace claim is inferred from these numbers.
    from trusted_router import async_settle_shadow_v2_a2_evidence as a2
    from trusted_router.async_settle_shadow_v2_a1_evidence import HANDOFF_ACCOUNTING
    from trusted_router.async_settle_shadow_v2_a2_roots import entry_root
    from trusted_router.async_settle_shadow_v2_gate_inputs import SHUTDOWN_SOURCE
    close_raw = db.rows[CLOSURE_KIND, DAY + '/' + boot].body.encode()
    result_raw = db.rows[CLOSE_RESULT_KIND, DAY + '/' + boot].body.encode()
    result = json.loads(result_raw)
    measurement = dict(**TAG, day=DAY, boot=boot, grace_us=9000000,
        final_pass_us=result['elapsed_us'], close_us=result['elapsed_us'], result_us=0,
        export_wait_us=shutdown_wait_us, preparation_us=0, outcome='graceful')
    source = file('sources/measurement/test-scheduler.json', SHUTDOWN_SOURCE.serialize(measurement))
    measured = file('collector/shutdown-measurement.json', a2.SHUTDOWN_MEASUREMENT.serialize({**measurement, 'source': source}))
    entry = dict(day=DAY, boot=boot, termination_applies=True, reason='shutdown', interval_end_us=end,
        grace_us=9000000, original_close=file('collector/original-close.json', close_raw),
        close_result=file('collector/original-close-result.json', result_raw), measurement=measured)
    page_raw = a2.ATTACHMENT_PAGE.serialize(dict(**TAG, page_seq=f'{1:020}', previous_sha256=None, entries=[entry]))
    file('collector/shutdown-page.json', page_raw)
    detail = dict(root=entry_root([entry], stream='shutdown_writers'), count=1, pages=1, tail_sha256=digest(page_raw))
    measurement_list = file('collector/shutdown-measurements.json', canonical([measured]))
    shutdown = dict(**TAG, writers=detail, measurements=measurement_list,
        approval=approval('reviews/shutdown.json', measurement_list), complete=True)
    file('proof.shutdown_inventory_sha256', a2.SHUTDOWN_INVENTORY.serialize(shutdown))
    aliases = [dict(kind='shutdown_entry', id='shutdown/' + DAY + '/' + boot, raw=a2.SHUTDOWN_ENTRY.serialize(entry).decode()),
        dict(kind='shutdown_measurement', id=measured['path'], raw=files[measured['path']].decode()),
        dict(kind='handoff_accounting', id=DAY + '/' + boot,
             raw=HANDOFF_ACCOUNTING.serialize(dict(v=2, policy_version='shadow-v2', contract_revision='a1',
                 day=DAY, boot=boot, expected_handoffs=1, attempts_lower=1, attempts_upper=1,
                 landed=True, reply_status='not_proven', failures_lower=0, failures_upper=1,
                 unknown_reply_upper=1, result_sha256=digest(result_raw))).decode())]
    public = canonical([asdict(signer().trusted)])
    ports = shadow_v2_ports(b'[]', public)
    for approved in ports.approvals:
        for raw in (approved.source, approved.fixture, approved.keyset):
            if len(raw) <= 262144:
                file('release/' + digest(raw), raw)
    # No pre-existing v1 observations or reviewed resets exist in this fresh
    # simulated deployment. Capture the real retained callable on that history.
    from scripts.async_settle.async_settle_shadow_v2_gate_tools import run_retained
    from trusted_router.async_settle_shadow_v2_gate_inputs import RETAINED_OUTPUT
    from trusted_router.async_settle_shadow_v2_gate_retained import capture_path
    empty_detail = dict(root=digest(b'[]'), count=0, pages=0, tail_sha256=None)
    payload = dict(rows=[], days=[DAY], proof={}, resets=[], serving=[[start, end, REVISION]],
        manifests={DAY: {'router_revisions': [REVISION]}})
    payload_ref = file('collector/retained-history.json', canonical(payload))
    retained = run_retained('shadow_report.collect_rollbacks', payload, {})
    output_ref = file('collector/retained-history-output.json', retained.output)
    file(capture_path(payload_ref['sha256']), RETAINED_OUTPUT.serialize(dict(
        entry_point='shadow_report.collect_rollbacks', payload_sha256=payload_ref['sha256'], output=output_ref)))
    source_bytes = next(a.source for a in ports.approvals if a.entry_point.endswith('.collect_rollbacks'))
    delegated = dict(**TAG, entry_point='shadow_report.collect_rollbacks', payload=payload_ref,
        dependencies=empty_detail, source=file('release/' + digest(source_bytes), source_bytes))
    file('proof.resolution_dag_sha256', a2.RESOLUTION_DAG.serialize(dict(**TAG,
        retained=delegated, history=empty_detail, approvals=[])))
    # Honest empty collection: no CI, oracle or production trial ran in this test.
    file('proof.external_proofs_sha256', a2.EXTERNAL_PROOFS.serialize(dict(**TAG, requirements=[], pilot=None)))
    plan = canonical(dict(artifacts={k: digest(v) for k, v in files.items()}, criteria={},
        verifiers={a.entry_point: [a.source_pin, a.fixture_pin, digest(a.keyset) if a.keys else None]
                   for a in ports.approvals}, required=[]))
    release = decode_release_plan(plan, digest(plan))  # Independent test-operator trust input.
    file(release.path, plan)
    from trusted_router.async_settle_shadow_v2_gate_release import measurement_parsers
    ports = replace(ports, release=release, measurements=measurement_parsers(release))
    # An explicitly incomplete proof makes diagnostics renderable without a
    # forged PASS or nonexistent measured artifact. Missing commitments differ.
    proof = {name: digest(('uncollected:' + name).encode()) for name in PROOF_MANIFEST.fields}
    proof.update(v=2, policy_version='shadow-v2', contract_revision='a1',
        source_shas=dict(router=REVISION, enclave='b' * 40),
        rosters_by_day={DAY: dict(router={boot: dict(revision=REVISION, region=region,
            lifetime=dict(start_us=start, end_us=end))}, enclave={})},
        instance_regions_by_day={DAY: {boot: region}}, attachments=[], explicit_keys=[], complete=False,
        cohort_seed_policy_sha256=digest(files['proof.cohort_seed_policy_sha256']),
        shutdown_inventory_sha256=digest(files['proof.shutdown_inventory_sha256']),
        resolution_dag_sha256=digest(files['proof.resolution_dag_sha256']),
        external_proofs_sha256=digest(files['proof.external_proofs_sha256']))
    for field, pin in ports.approved_files.items():
        if field.startswith('proof.'):
            proof[field.split('.')[1]] = pin
    return dict(proof=PROOF_MANIFEST.serialize(proof).decode(), attachments=aliases,
        preimages=[dict(name=k, raw=v.decode()) for k, v in files.items()]), ports


def collector_day_manifest(db, supplied, boot, end, region):
    """Missing manifest producer stand-in from this test's independent roster.

    Rows/receipts are original writes. Uncollected roots remain unresolved;
    complete=False prevents this diagnostic manifest claiming certification.
    """
    from trusted_router.async_settle_shadow_v2_a1_evidence import MANIFEST
    from trusted_router.async_settle_shadow_v2_a2_evidence import L
    from trusted_router.async_settle_shadow_v2_a2_roots import _root_entry, entry_root
    manifest = {name: digest(('uncollected:' + name).encode()) for name in MANIFEST.fields}
    manifest.update(v=2, policy_version='shadow-v2', contract_revision='a1', day=DAY,
        router_writers={boot: dict(revision=REVISION, region=region, lifetime=dict(start_us=START, end_us=end))},
        enclave_writers={}, revisions=dict(router=[REVISION], enclave=[]),
        first_evidence_at_us=START, proof_manifest_sha256=digest(supplied['proof'].encode()),
        disabled_flag_intervals=[dict(interval=dict(start_us=START, end_us=end),
            admission=False, protection=False, negotiation=False, fast_drain=False)],
        complete=False, gaps=[])
    for name, kind in (('counter_root', COUNTER_KIND), ('closure_root', CLOSURE_KIND), ('receipts_root', PAGE_KIND)):
        entries = [_root_entry(L(*key.split('/')), [row.body.encode()])
            for (row_kind, key), row in db.rows.items() if row_kind == kind]
        manifest[name] = entry_root(entries, stream=name)
    inventory = next(p['raw'] for p in supplied['preimages'] if p['name'] == DAY + '/manifest.independent_inventory_sha256')
    manifest['independent_inventory_sha256'] = digest(inventory.encode())
    for name in ('config_sha256', 'fixture_sha256'):
        manifest[name] = digest(next(p['raw'].encode() for p in supplied['preimages'] if p['name'] == 'manifest.' + name))
    return OriginalRow(DAY + '/manifest-v2', MANIFEST.serialize(manifest),
        {'test_collector': 'independent-test-roster-and-original-row-roots'})


@pytest.mark.asyncio
async def test_real_writers_export_load_evaluate_render(monkeypatch, tmp_path):
    mono, wall = [100.], [START]
    monkeypatch.setattr('time.time_ns', lambda: wall[0] * 1000)
    store, db = fake_store()
    store.clock, store.wall = lambda: mono[0], lambda: wall[0] / 1000000
    cfg = Settings(environment='test', source_revision=REVISION,
        async_settle_shadow_policy='shadow-v2', async_settle_shadow_workspaces='ws-v1',
        async_settle_shadow_health_region='us-central1', region='us-central1')
    sink = ObjectSink()
    with monkeypatch.context() as local:
        local.setattr('trusted_router.async_settle_shadow_evidence.uuid.uuid4', lambda: '11111111-1111-4111-8111-111111111111')
        service = Service(cfg, store, sink)
    service.observer.evidence_wall = store.wall
    bookings = {}
    shell = Runtime(cfg, runtime(), service.port,
        SimpleNamespace(booking=lambda key, deadline: bookings[key]), typed=True)
    shell.signer = signer()
    tokens = []
    try:
        # Allocate through the real store, including its real cap control row.
        service.maintain()
        for index, kind in enumerate(('settle', 'refund', 'settle')):
            wall[0], mono[0] = START + index * 2000000, 100. + index * 2
            db.server_ns = wall[0] * 1000
            service.publish_cycle(wall[0] // 2000000, wall[0])
            service.observer.install_health(mono[0], store.read_shadow_health())
            # Fake credit/trust state is a controlled test input, not evidence.
            service.observer.install_workspace('ws-v1', mono[0], Admission(0, 2))
            auth = authorization()
            auth.id, auth.invocation_nonce, auth.credit_reservation_id = f'auth-{index}', f'nonce-{index}', f'res-{index}'
            auth.created_at = '2026-10-12T00:00:00+00:00'
            auth.endpoint_id = endpoint().id
            auth.candidate_endpoint_ids = [endpoint().id]
            snapshot = project((endpoint(),), auth.created_at)
            values = dict(billing_snapshot=snapshot.model_dump(mode='json'),
                billing_snapshot_hash=billing.canonical_hash(snapshot), async_eligible=False)
            authorize_body = GatewayAuthorizeRequest(api_key_hash=auth.key_hash, model=auth.model_id,
                route_type='chat.completions', stream=False, requested_parameters=[])
            shell.authorize_http(request('X-TR-Settlement-Shadow-Policy', 'shadow-v2'),
                authorize_body, auth, values, [endpoint()], typed=True, federated=False, replay=False)
            shell.join()
            assert 'billing_shadow_binding' in values
            token = values['billing_shadow_binding']
            claims = verify_binding(token, [shell.signer.trusted], wall[0] // 1000000).model_dump()
            raw_usage = billing.RawUsage(input_tokens=1, output_tokens=1)
            observed = billing.Eligibility()
            evaluated = billing.evaluate(snapshot, endpoint().id, raw_usage, observed)
            charge = evaluated.charge_micro if kind == 'settle' else 0
            terminal = billing.TerminalEnvelope(**{k: claims[k] for k in ('authorization_id',
                'generation_id', 'workspace_id', 'key_id', 'invocation_nonce', 'billing_authority',
                'journal_region', 'epoch', 'snapshot_version', 'snapshot_hash', 'route_type', 'streamed')},
                v=1, selected_endpoint=endpoint().id, usage=evaluated.usage, charge_micro=charge, terminal_kind=kind)
            wire = dict(v=2, policy_version='shadow-v2', variant='compared',
                billing_shadow_binding=token, billing_snapshot=values['billing_snapshot'],
                raw_usage=raw_usage.model_dump(), observed=observed.model_dump(),
                terminal=terminal.model_dump(), payload_hash=billing.canonical_hash(terminal),
                go_error=None, go_evaluator='billing-v1', go_revision='b' * 40, handoff_prepare_us=1)
            header = b64encode(COMPARED_FULL.serialize(wire))
            body = GatewaySettleRequest(authorization_id=auth.id, actual_input_tokens=1,
                actual_output_tokens=1, selected_endpoint=endpoint().id, route_type='chat.completions')
            bookings[auth.id] = Booking(charge, 'settled' if kind == 'settle' else 'refunded', True)
            capture, size = shell.capture_terminal(request('X-TR-Settlement-Shadow', header), body, auth, kind)
            assert capture is not None
            capture.endpoint = endpoint()
            capture.endpoints = (endpoint(),)
            capture.prices_match_booking = True
            shell.process(capture)
            shell.release(size)
            assert capture.verified.partition == 'A', capture.verified
            assert capture.verified.comparison.classification == 'exact'
            tokens.append(dict(object_id=DAY + '/' + auth.id, verifier='binding', token=token,
                now=wall[0] // 1000000, expected={}, attempt={}, server_attempt={}, claims={}))
        shell.close()
        shell.worker.join(2)
        # A final partial slot is closed, not claimed as an unobserved full day.
        wall[0] += 100000
        mono[0] += .1
        db.server_ns = wall[0] * 1000
        import asyncio
        from contextlib import nullcontext

        from trusted_router.services import async_settle_shadow_health as health_module
        async def sleep(seconds):
            mono[0] += seconds
            await asyncio.sleep(0)
        scheduler = SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio)})
        scheduler.sleep = sleep
        scheduler.timeout = lambda seconds: nullcontext()
        monkeypatch.setattr(health_module, 'asyncio', scheduler)
        shutdown_started = mono[0]
        await service.close()
        assert not service.unclosed, (service.state, service.final_veto, service.journal.days, service.bases.queue, service.traces.queue, service.port.fold_obligations)
        assert {(kind, DAY + '/11111111-1111-4111-8111-111111111111') for kind in (COUNTER_KIND, CLOSURE_KIND, CLOSE_RESULT_KIND)} <= db.rows.keys()
        pages = [json.loads(row.body) for (kind, _), row in db.rows.items() if kind == PAGE_KIND]
        events = [event for page in pages for event in page['events']]
        assert sum(e['phase'] == 'authorize' for e in events) == 3
        assert [(e['phase'], e['partition'], e['comparison']) for e in events if e['phase'] in ('settle', 'refund')] == [('settle', 'A', 'exact'), ('refund', 'A', 'exact'), ('settle', 'A', 'exact')]
        assert sum(kind == SAMPLE_KIND for kind, _ in db.rows) == 3
        assert {key.split('/')[0] for key in sink.objects} == {'health-basis-batches', 'publisher-traces'}
        supplied, ports = operator_collector_standins('11111111-1111-4111-8111-111111111111', START, wall[0], cfg.effective_serving_region, tokens, db, round((mono[0] - shutdown_started) * 1000000))
        # Test-owned capture inventory, explicitly not a production independent collector.
        # Independent of the object listing: the controlled first checkpoint
        # drains slot zero; shutdown drains one basis batch and slots one/two.
        # Deleting an upload cannot delete its expected key from this plan.
        prefix = DAY + '/' + service.journal.boot + '/'
        keys = ('publisher-traces/' + prefix + str(START // 2000000),
                'health-basis-batches/' + prefix + f'{1:020}',
                'publisher-traces/' + prefix + str(START // 2000000 + 1))
        assert set(sink.objects) == set(keys)
        port = MemoryPort.from_fake(db, sink.objects, object_keys=keys, page_size=2)
        control = DAY_KINDS[5]
        port.originals[control] = (*port.originals.get(control, ()),
            collector_day_manifest(db, supplied, service.journal.boot, wall[0], cfg.effective_serving_region))
        exported = export_day(port, DAY, completed_at_us=wall[0] + 1000000,
            object_keys=keys, supplied=supplied)
        assert not exported.gaps
        raw = json.loads(exported.transport)
        for row in raw['rows']:
            if not row['id'].endswith('/manifest-v2'):
                assert row['raw'].encode() == db.rows[row['kind'], row['id']].body.encode()
        for obj in raw['attachments']:
            if obj['id'] in sink.objects:
                assert obj['raw'].encode() == sink.objects[obj['id']].body
        bundle = load(exported.transport, ports)
        assert bundle.shutdown is not None
        # DiagnosticCapIsolation proves the retained history itself is valid:
        # reset_view currently parses every control row as a day MANIFEST.
        cap_isolated = json.loads(exported.transport)
        cap_isolated['rows'] = [r for r in cap_isolated['rows'] if not r['id'].endswith('/cap-v2')]
        assert load(canonical(cap_isolated), ports).reset_support is not None
        result = evaluate(bundle, ports, gate='transport')
        rendered = render(result)
        assert isinstance(rendered, tuple)
        assert json.loads(rendered[0])['verdict'] == 'BLOCKED'
        # MissingManifestCapRecovery: an absent collector must not erase proof
        # siblings. Preserve the exact observed defect until its owner fixes it.
        missing_manifest = json.loads(exported.transport)
        missing_manifest['rows'] = [r for r in missing_manifest['rows'] if not r['id'].endswith('/manifest-v2')]
        damaged = load(canonical(missing_manifest), ports)
        from trusted_router.async_settle_shadow_v2_gate_result import UnavailableGateOutput
        raw_result = evaluate(damaged, ports, gate='transport')
        raw_rendered = render(raw_result)
        assert isinstance(raw_rendered, UnavailableGateOutput)
        assert raw_rendered.condition == 'proof_manifest_absent_or_undecodable'
        assert damaged.a2_streams['loading_issues'] == [dict(gap='evidence_schema_gap',
            path='transport/container', detail="'router_writers'")]
        assert raw_result.gaps == RAW_EXPECTED_GAPS
        diagnostic = dict(loading=list(bundle.a2_streams.get('loading_issues', [])),
            validation=list(bundle.a2_streams.get('loading_validation', [])),
            gaps=list(result.gaps), failures=[dict(gap=f.gap, detail=f.detail, count=f.count, subject=f.subject) for f in result.failures])
        (tmp_path / 'diagnostics.json').write_bytes(canonical(diagnostic))
        # Round-1 audit artifact, reproducible without production access.
        audit_artifact('diagnostics.json', canonical(diagnostic))
        audit_artifact('transport.json', exported.transport)
        audit_artifact('gate-output.json', rendered[0])
        audit_artifact('gate-report.json', rendered[1][0])
        assert result.gaps == EXPECTED_GAPS
        report_counts = json.loads(rendered[1][0])['failure_counts']
        assert {k: v['count'] for k, v in report_counts.items() if v['count']} == {
            'artifact_gap': 14, 'evidence_schema_gap': 4}
        assert {(f.gap, f.detail) for f in result.failures} == EXPECTED_FAILURE_DETAILS
        assert tuple((v['gap'], v['path'], v['detail']) for v in diagnostic['loading']) == EXPECTED_LOADING
        from collections import Counter
        assert Counter((v['gap'], v['detail']) for v in diagnostic['validation']) == EXPECTED_VALIDATION
        # Verify every stored field at the exact same public loader boundary.
        from trusted_router.async_settle_shadow_v2_a1_evidence import validate_row
        for row in raw['rows']:
            validate_row(row['kind'], row['id'], row['raw'].encode())
        counter = json.loads(db.rows[COUNTER_KIND, DAY + '/' + service.journal.boot].body)
        assert (counter['A'], counter['B'], counter['C'], counter['eligible_demand']) == (3, 0, 0, 3)
        assert counter['publisher']['publications'] == 3
        assert counter['publisher']['failures'] == 0

    finally:
        shell.close()
        shell.worker.join(2)
        service.publisher_executor.shutdown(wait=True, cancel_futures=True)
        service.evidence_executor.shutdown(wait=True, cancel_futures=True)
        if service.observer.executor is not None:
            service.observer.executor.shutdown(wait=True, cancel_futures=True)


RAW_EXPECTED_GAPS = ('artifact_gap', 'evidence_schema_gap')
EXPECTED_GAPS = ('artifact_gap', 'evidence_schema_gap')  # Filled from the real-writer first integration, not generated evidence.


def test_export_preserves_bytes_pages_conflicts_and_failure_siblings():
    kind = DAY_KINDS[0]
    rows = tuple(OriginalRow(DAY + f'/auth-{i}', b'{ "x": 1 }\n', {'fake_version': i}) for i in range(4))
    keys = (f'publisher-traces/{DAY}/boot/1', f'publisher-traces/{DAY}/boot/2')
    second = OriginalObject(keys[1], b'{}\n', '7')
    port = MemoryPort({kind: rows}, {keys[1]: (second,)}, keys, page_size=2)
    result = export_day(port, DAY, completed_at_us=START, object_keys=keys)
    value = json.loads(result.transport)
    assert [r['raw'].encode() for r in value['rows']] == [r.body for r in rows]
    assert value['attachments'][0]['raw'].encode() == second.body
    pages = [p for p in port.pages if p.get('kind') == kind]
    assert [p['count'] for p in pages] == [2, 2, 0]
    assert pages[-1]['complete'] and pages[-1]['after'] == rows[-1].key
    assert result.gaps == ({'key': keys[0], 'gap': 'artifact_gap', 'reason': 'FileNotFoundError:read_failed'},)
    duplicate = MemoryPort({kind: (rows[0], replace(rows[0], body=b'{}'))}, {}, (), page_size=2)
    conflict = export_day(duplicate, DAY, completed_at_us=START, object_keys=())
    assert len(json.loads(conflict.transport)['rows']) == 2
    assert conflict.gaps[0]['reason'] == 'conflicting_originals'


def test_export_native_seams_fail_closed_and_invalid_bytes_are_retained():
    native = export_day(NativePort(), DAY, completed_at_us=START,
        object_keys=(f'publisher-traces/{DAY}/boot/1',))
    assert len(native.gaps) == len(DAY_KINDS) + 1
    row = OriginalRow(DAY + '/bad', b'\xff', {})
    invalid = export_day(MemoryPort({DAY_KINDS[0]: (row,)}, {}, ()), DAY,
        completed_at_us=START, object_keys=())
    assert invalid.originals[0][1] == b'\xff'
    assert invalid.gaps[0]['reason'] == 'non_utf8_original_retained_in_sidecar'


EXPECTED_LOADING = (('artifact_gap', 'proof.pre_budget_sha256', 'required payload absent'),
 ('artifact_gap', 'proof.post_budget_sha256', 'required payload absent'),
 ('evidence_schema_gap', 'proof.pre_budget_sha256', "'proof.pre_budget_sha256'"),
 ('artifact_gap', 'proof.external_proofs_sha256', 'artifact_gap'),
 ('evidence_schema_gap', 'proof.resolution_dag_sha256', 'evidence_schema_gap'))

EXPECTED_VALIDATION = {('artifact_gap', 'A2 manifest/proof preimage validation failed'): 1,
 ('artifact_gap', 'A2 required original preimage absent'): 2,
 ('artifact_gap', 'artifact_gap'): 1,
 ('artifact_gap', 'coverage expected-set mismatch: close_result'): 1,
 ('artifact_gap', 'expected prerequisite ledger absent'): 1,
 ('artifact_gap', 'incomplete proof'): 1,
 ('artifact_gap', 'missing expected coverage pages'): 5,
 ('artifact_gap', 'required payload absent'): 2,
 ('evidence_schema_gap', "'proof.pre_budget_sha256'"): 1,
 ('evidence_schema_gap', 'evidence_schema_gap'): 1,
 ('evidence_schema_gap', 'unexpected source outside expected population'): 2}


@pytest.mark.parametrize('page_size', [1, 2, 200])
def test_accounting_hashes_originals_and_follows_all_pages(page_size):
    kind = DAY_KINDS[0]
    rows = tuple(OriginalRow(DAY + f'/a-{i:04}', (' {"i":' + str(i) + '}\n').encode(),
                             {'fake_version': i}) for i in range(201))
    result = export_day(MemoryPort({kind: rows}, {}, (), page_size=page_size), DAY,
        completed_at_us=START, object_keys=())
    assert not result.gaps
    value, accounting = json.loads(result.transport), json.loads(result.accounting)
    files = {v['name']: v['raw'].encode() for v in value['preimages']}
    records = []
    for stream, refs in accounting['details'].items():
        previous = None
        for ref in refs:
            raw = files[ref['path']]
            assert len(raw) == ref['bytes'] <= 262144 and digest(raw) == ref['sha256']
            page = json.loads(raw)
            assert page['previous_sha256'] == previous
            previous = digest(raw)
            if stream == 'entries':
                records.extend(page['entries'])
    assert [(r['key'], r['bytes'], r['sha256']) for r in records] == [(r.key, len(r.body), digest(r.body)) for r in rows]
    assert accounting['source_body_bytes'] == sum(len(r.body) for r in rows)
    assert accounting['entry_count'] == len(rows)


def test_bound_exhaustion_accounts_entire_already_read_page():
    kind = DAY_KINDS[0]
    rows = tuple(OriginalRow(DAY + f'/a-{i}', b'{}', {}) for i in range(5))
    port = MemoryPort({kind: rows}, {}, (), page_size=2)
    result = export_day(port, DAY, completed_at_us=START, object_keys=(), max_objects=1)
    assert not json.loads(result.accounting)['complete']
    assert [raw for _, raw in result.originals] == [b'{}', b'{}']
    assert len(port.pages) == 1
    assert result.gaps[0]['reason'] == 'finite_export_bound_exhausted'


def test_cli_offline_capture_and_exclusive_output(monkeypatch, tmp_path, capsys):
    import sys

    from scripts.async_settle.shadow_v2_live_export import main
    capture = tmp_path / 'capture.json'
    capture.write_bytes(canonical(dict(rows=[dict(kind=DAY_KINDS[0], key=DAY + '/a',
        raw=' {"original":true}\n', commit={'fake_version': 1})], objects=[], object_keys=[])))
    bundle = tmp_path / 'bundle.json'
    monkeypatch.setattr(sys, 'argv', ['shadow_v2_live_export', '--live', '--capture', str(capture),
        '--day', DAY, '--completed-at-us', str(START), '--bundle', str(bundle)])
    assert main() == 0
    assert json.loads(bundle.read_bytes())['rows'][0]['raw'] == ' {"original":true}\n'
    assert json.loads(capsys.readouterr().out)['export_complete']
    with pytest.raises(FileExistsError):
        main()


EXPECTED_FAILURE_DETAILS = {('artifact_gap', 'A2 manifest/proof preimage validation failed'),
 ('artifact_gap', 'artifact_gap'),
 ('artifact_gap', 'expected prerequisite ledger absent'),
 ('artifact_gap', 'incomplete proof'),
 ('artifact_gap', 'missing expected coverage pages'),
 ('artifact_gap', 'required payload absent'),
 ('artifact_gap', 'undefined preimage: budget'),
 ('artifact_gap', 'undefined preimage: external'),
 ('artifact_gap', 'undefined preimage: reset_support'),
 ('evidence_schema_gap', "'proof.pre_budget_sha256'"),
 ('evidence_schema_gap', 'evidence_schema_gap'),
 ('evidence_schema_gap', 'unexpected source outside expected population')}
