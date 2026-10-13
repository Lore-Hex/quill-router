"""Real P3/P4a writer outputs and C1 source captures; no gate fixture generation."""
from __future__ import annotations

import json
from types import SimpleNamespace

from tests.test_async_settle_shadow import signer
from tests.test_async_settle_shadow_observation import fake_store
from tests.test_async_settle_shadow_v2_live_export import DAY, REVISION, START, ObjectSink, request
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
from trusted_router.async_settle_shadow_v2_contract import COMPARED_FULL
from trusted_router.async_settle_shadow_v2_export import (
    digest,
    export_day,
)
from trusted_router.async_settle_shadow_v2_gate_loader import load
from trusted_router.config import Settings
from trusted_router.detached_jws import b64encode, canonical
from trusted_router.schemas import GatewayAuthorizeRequest, GatewaySettleRequest
from trusted_router.services.async_settle import Admission
from trusted_router.services.async_settle_shadow_cohort_runtime import Runtime
from trusted_router.services.async_settle_shadow_health import Service


async def test_real_writers_native_export_collect_and_pack_loader(monkeypatch, tmp_path):
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
            claims = verify_binding(values['billing_shadow_binding'], [shell.signer.trusted], wall[0] // 1000000).model_dump()
            raw_usage = billing.RawUsage(input_tokens=1, output_tokens=1)
            observed = billing.Eligibility()
            evaluated = billing.evaluate(snapshot, endpoint().id, raw_usage, observed)
            charge = evaluated.charge_micro if kind == 'settle' else 0
            terminal = billing.TerminalEnvelope(**{k: claims[k] for k in ('authorization_id',
                'generation_id', 'workspace_id', 'key_id', 'invocation_nonce', 'billing_authority',
                'journal_region', 'epoch', 'snapshot_version', 'snapshot_hash', 'route_type', 'streamed')},
                v=1, selected_endpoint=endpoint().id, usage=evaluated.usage, charge_micro=charge, terminal_kind=kind)
            wire = dict(v=2, policy_version='shadow-v2', variant='compared',
                billing_shadow_binding=values['billing_shadow_binding'], billing_snapshot=values['billing_snapshot'],
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
        await service.close()
        assert not service.unclosed, (service.state, service.final_veto, service.journal.days, service.bases.queue, service.traces.queue, service.port.fold_obligations)
        assert {(kind, DAY + '/11111111-1111-4111-8111-111111111111') for kind in (COUNTER_KIND, CLOSURE_KIND, CLOSE_RESULT_KIND)} <= db.rows.keys()
        pages = [json.loads(row.body) for (kind, _), row in db.rows.items() if kind == PAGE_KIND]
        events = [event for page in pages for event in page['events']]
        assert sum(e['phase'] == 'authorize' for e in events) == 3
        assert [(e['phase'], e['partition'], e['comparison']) for e in events if e['phase'] in ('settle', 'refund')] == [('settle', 'A', 'exact'), ('refund', 'A', 'exact'), ('settle', 'A', 'exact')]
        assert sum(kind == SAMPLE_KIND for kind, _ in db.rows) == 3
        assert {key.split('/')[0] for key in sink.objects} == {'health-basis-batches', 'publisher-traces'}
        from tests.test_async_settle_shadow_v2_collect import inventory, review, setup
        from tests.test_async_settle_shadow_v2_export_native import Database, native
        from trusted_router.async_settle_shadow_v2_collect_ports import Files
        from trusted_router.async_settle_shadow_v2_pack_coverage import (
            ExactPages,
            handoff,
            receipt_chain,
        )
        from trusted_router.async_settle_shadow_v2_pack_daypack import compare_export_accounting
        from trusted_router.async_settle_shadow_v2_pack_plan import (
            PackFiles,
            check_plan,
            input_plan,
        )
        # Reuse the real writer's rows at the native exporter port boundary. The
        # test database supplies explicit fake snapshot/RPC records.
        native_db = Database([(kind, key, row.body) for (kind, key), row in db.rows.items()])
        exported = export_day(native(native_db, page_size=2), DAY, completed_at_us=wall[0],
                              object_keys=(), spool_dir=tmp_path / 'native')
        root_ref = dict(path='accounting.json', sha256=digest(exported.accounting), bytes=len(exported.accounting))
        compared = compare_export_accounting(Files(tmp_path / 'native'), root_ref,
            exported.transport_path.read_bytes(), day=DAY, object_plan={})
        assert compared.complete, compared.missing
        # C1 reads explicit fake lifecycle records, including a zero-row boot.
        captured, binding, fake, queries = setup(tmp_path / 'collector', names=(service.journal.boot, 'idle-boot'))
        run, first = inventory(captured, binding, fake, queries)
        assert first.complete, run.missing
        supplied_review = review(captured, first.deployment)
        run, collected = inventory(captured, binding, fake, queries, supplied_review)
        assert collected.complete and len(collected.boots) == 2
        pack = PackFiles(tmp_path / 'pack')
        # Test-owned enumeration of all files emitted by the two bounded passes.
        for base, prefix in ((tmp_path / 'native', 'native/'), (captured.root, 'collector/')):
            for path in sorted(base.rglob('*')):
                if path.is_file() and path.name not in ('bundle.json', 'rows.jsonl', 'attachments.jsonl'):
                    pack.put(prefix + path.relative_to(base).as_posix(), path.read_bytes())
        counter = json.loads(db.rows[COUNTER_KIND, DAY + '/' + service.journal.boot].body)
        originals = {key: (row.body.encode(),) for (kind, key), row in db.rows.items() if kind == PAGE_KIND}
        receipts = receipt_chain(ExactPages(lambda key: originals.get(key, ())), counter)
        assert receipts.tail is None and len(receipts.pages) > 0
        original_result = json.loads(db.rows[CLOSE_RESULT_KIND, DAY + '/' + service.journal.boot].body)
        packed_handoff = handoff(DAY, service.journal.boot, original_result)
        pack.obj('handoff.json', packed_handoff)
        from trusted_router import async_settle_shadow_v2_a1_reconciliation as joins
        from trusted_router.async_settle_shadow_v2_pack_coverage import (
            contender_chain,
            coverage,
            obligations,
        )
        original_close = json.loads(db.rows[CLOSURE_KIND, DAY + '/' + service.journal.boot].body)
        terminals = [(page, event) for page in receipts.pages for event in page['events']
                     if event['phase'] in ('settle', 'refund')]
        def terminal(entry):
            found = [(page, event) for page, event in terminals if digest(canonical(event['identity'])) == entry['identity_sha256']]
            assert len(found) == 1
            page, event = found[0]
            return page, joins.event_locator(page, event['seq'])
        obligation_pages, projection, missing = obligations(pack, original_close, list(receipts.pages),
            original=lambda reference: None, terminal=terminal, complete_through_us=START + 172805000000)
        assert not missing and projection['open_obligation_count'] == 0
        assert [entry['horizon_at_us'] for page in obligation_pages for entry in page['entries']] == [
            START + i * 2000000 + 172800000000 for i in range(3)]
        objects = {key: (item.body,) for key, item in sink.objects.items()}
        traces = contender_chain(ExactPages(lambda key: objects.get(key, ())), day=DAY,
                                 boot=service.journal.boot, start_us=START, end_us=wall[0])
        assert traces.tail is None
        records = {'close_result/' + DAY + '/' + service.journal.boot: (db.rows[CLOSE_RESULT_KIND, DAY + '/' + service.journal.boot].body.encode(),),
                   'handoff_accounting/' + DAY + '/' + service.journal.boot: (canonical(packed_handoff),)}
        expected = dict(close_result=[DAY + '/' + service.journal.boot],
                        handoff_accounting=[DAY + '/' + service.journal.boot], publisher_trace=list(traces.keys),
                        health_basis=sorted(key for key in objects if key.startswith('health-basis-batches/')),
                        authorization_obligation=[])
        for kind in ('publisher_trace', 'health_basis'):
            for key in expected[kind]:
                records[kind + '/' + key] = objects[key]
        for page in obligation_pages:
            key = DAY + '/' + service.journal.boot + '/' + page['page_seq']
            expected['authorization_obligation'].append(key)
            records['authorization_obligation/' + key] = (canonical(page),)
        committed = coverage(pack, ExactPages(lambda key: records.get(key, ())), day=DAY, expected=expected)
        assert all(entry['state'] == 'present' for pages in committed.values() for page in pages for entry in page['entries'])
        planned = input_plan(pack, list(pack.refs.values()), days=[DAY], predecessors=[])
        assert check_plan(pack, planned)
        # Native source records contain a cap, but no invented day manifest or
        # placeholder budget. Current loader loses scope here; preserve exactly.
        loaded = load(exported.transport_path.read_bytes())
        assert loaded.proof is None
        assert loaded.a2_streams['loading_issues'] == [dict(gap='evidence_schema_gap',
            path='transport/container', detail="'router_writers'")]
        assert not any('signatures' in ref['path'] for ref in pack.refs.values())
    finally:
        shell.close()
        shell.worker.join(2)
        service.publisher_executor.shutdown(wait=True, cancel_futures=True)
        service.evidence_executor.shutdown(wait=True, cancel_futures=True)
        if service.observer.executor is not None:
            service.observer.executor.shutdown(wait=True, cancel_futures=True)
