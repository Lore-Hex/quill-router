"""Startup hold through the existing compared runtime and native PAGE writer."""
from __future__ import annotations

import json
from dataclasses import replace

import pytest

from tests.test_async_settle_shadow import NOW, context
from tests.test_async_settle_shadow_cohort import additions
from tests.test_async_settle_shadow_cohort_runtime import installed, request
from tests.test_async_settle_shadow_startup_harness import descriptor, fake_harness
from tests.test_async_settle_ticket import endpoint
from trusted_router.async_settle_shadow_v2_evidence import EVENT
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services.async_settle_shadow_startup_harness import Admission


@pytest.mark.parametrize('ready', [False, True])
@pytest.mark.parametrize('member', [False, True])
def test_authorize_http_readiness_and_noncohort_unchanged(monkeypatch, ready, member):
    shell, service, _ = installed()
    shell.startup_admission = Admission(ready)
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    ctx = context()
    auth = ctx.authorization if member else replace(ctx.authorization, workspace_id='outside')
    body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=auth.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    values = additions()
    before = dict(values)
    try:
        shell.authorize_http(request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2'),
            ('incoming-header', 'request payload that must never appear')]), body, auth, values, [endpoint()],
            typed=True, federated=False, replay=False)
        shell.join()
        events = [EVENT.parse(s.body) for p in service.journal.pages for s in p.slots if s.body]
        assert bool(values.get('billing_shadow_binding')) == (ready and member)
        if member:
            assert len(events) == 1
            assert events[0]['event_at_us'] == NOW*1000000
            if not ready:
                assert events[0]['eligibility'] == 'unknown'
                assert events[0]['diagnostic']['reason'] == 'missing_context'
                assert values == before
        else:
            assert not events and values == before
    finally:
        shell.close()
        shell.worker.join(2)


def test_attempt_uses_native_start_and_exact_closed_page(monkeypatch):
    shell, service, _ = installed()
    shell.startup_admission = Admission(False)
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash='key-v1', model=ctx.authorization.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    h, lines, _ = fake_harness()
    h.boot = service.journal.boot
    h.correlate(descriptor())
    try:
        shell.authorize_http(request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2')]), body,
            ctx.authorization, additions(), [endpoint()], typed=True, federated=False, replay=False)
        shell.join()
        pages = service.journal.freeze(next(iter(service.journal.days)))
        assert len(pages) == 1
        monkeypatch.setattr('time.time_ns', lambda: (NOW+100)*10**9)
        h.retained_page(pages[0], {int(pages[0].value()["events"][0]["seq"]): "rejected"})
        value = json.loads(lines[-1])
        assert value['kind'] == 'a3_opted_attempt'
        assert value['result'] == 'rejected'
        assert value['at_us'] == NOW*1000000
        assert value['receipt']['original']['sha256'] == pages[0].digest
        assert value['receipt']['original']['bytes'] == len(pages[0].body)
        assert value['receipt']['sequence'] == int(pages[0].value()['events'][0]['seq'])
    finally:
        shell.close()
        shell.worker.join(2)


async def test_serving_runner_starts_harness_after_listener(monkeypatch):
    from types import SimpleNamespace

    import uvicorn

    from trusted_router import serve
    from trusted_router.services import async_settle_shadow_startup_harness as module
    order = []
    async def startup(self, sockets=None):
        order.append('listener')
        self.started = True
        self.lifespan = SimpleNamespace(app='app')
    monkeypatch.setattr(uvicorn.Server, 'startup', startup)
    monkeypatch.setattr(module, 'listener_started', lambda app: order.append('harness'))
    server = serve.DrainingServer(uvicorn.Config('unused'), drain_seconds=0)
    server.lifespan = SimpleNamespace(app='app')
    await server.startup()
    assert order == ['listener', 'harness']


def test_native_attempt_without_result_stays_missing():
    from types import SimpleNamespace
    h, lines, _ = fake_harness()
    page = dict(boot=h.boot, day='2026-10-13', events=[dict(phase='authorize',
        receipt_locator={'boot': h.boot}, seq='00000000000000000001', event_at_us=1)])
    h.retained_page(SimpleNamespace(value=lambda: page, body=b'retained-page'))
    assert not lines and 'NativeAttemptResults' in h.missing


def test_startup_without_p3_service_does_not_fail_application():
    from types import SimpleNamespace

    from tests.test_async_settle_shadow_cohort import config
    from trusted_router.services.async_settle_shadow_startup_harness import install
    app = SimpleNamespace(state=SimpleNamespace())
    install(app, config())
    assert app.state.async_settle_shadow_startup.boot
    assert callable(app.state.async_settle_shadow_startup_begin)


def stop_service(service):
    service.publisher_executor.shutdown(wait=True, cancel_futures=True)
    service.evidence_executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('contender', [False, True])
async def test_real_install_originals(monkeypatch, contender):
    from tests.test_async_settle_shadow_observation import NOW_US
    from tests.test_async_settle_shadow_observation import service as make_service
    from trusted_router.services.async_settle_shadow_startup_harness import bind_service
    from trusted_router.services.async_settle_shadow_startup_records import coordinate
    svc = make_service()
    svc.store.wall = lambda: NOW_US/1e6
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    h, _, _ = fake_harness()
    h.boot = svc.journal.boot
    h.boot_at_us = NOW_US-1
    h.now = lambda: NOW_US+1000
    bind_service(h, svc)
    try:
        svc.publish_cycle(NOW_US//2_000_000, NOW_US)
        assert svc.startup_publication is not None
        claim_raw = svc.store.startup_claim_raw
        publication_raw = svc.store.startup_publication_raw
        if not contender:
            svc.settings.async_settle_shadow_health_region = 'another-region'
        svc.observer.install_health(svc.store.clock(), svc.store.read_shadow_health())
        original = svc.startup_observation
        assert original is not None
        raw = original['health_basis_raw']
        assert json.loads(raw)['reader_boot'] == svc.journal.boot
        assert json.loads(raw)['source_state'] == 'installed'
        # A later cache edit must not replace or reconstruct the install buffer.
        svc.observer.health = None
        from tests.test_async_settle_shadow_startup_harness import manifest
        h.manifest = manifest()
        fields = await h.local_fields('observer_freshness', {})
        assert fields['health_basis'] == coordinate('health_basis', raw)
        assert h.originals.buffers[('health_basis', fields['health_basis']['sha256'], len(raw))] is raw
        assert fields['install_elapsed_us'] == 0
        if contender:
            assert original['claim_raw'] is claim_raw
            assert original['publication_raw'] is publication_raw
            assert fields['claim'] == coordinate('claim', claim_raw)
            assert fields['publication'] == coordinate('publication', publication_raw)
    finally:
        stop_service(svc)


@pytest.mark.parametrize('outcome', ['accepted', 'rejected', 'failed', 'timeout'])
async def test_real_runtime_attempt_result_and_acknowledged_page(monkeypatch, outcome):
    from trusted_router.services import async_settle_shadow_cohort_runtime as module
    from trusted_router.services.async_settle_shadow_startup_harness import bind_service
    shell, svc, _ = installed()
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    bind_service(h, svc, shell)
    shell.startup_admission = Admission(outcome != 'rejected')
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    if outcome in {'failed', 'timeout'}:
        def broken(*args):
            raise TimeoutError if outcome == 'timeout' else ValueError
        monkeypatch.setattr(module, 'compared_handoff_fields', broken)
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash='request-value-never-retained', model=ctx.authorization.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    try:
        shell.authorize_http(request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2')]), body,
            ctx.authorization, additions(), [endpoint()], typed=True, federated=False, replay=False)
        shell.join()
        assert h.attempt_count == 1 and h.missing_attempts == 1
        assert not lines
        monkeypatch.setattr('time.time_ns', lambda: (NOW+100)*10**9)
        pages = svc.journal.freeze(next(iter(svc.journal.days)))
        svc.flush(svc.store.clock()+2)
        rows = [json.loads(raw) for raw in lines if json.loads(raw)['kind'] == 'a3_opted_attempt']
        assert len(rows) == 1
        assert rows[0]['at_us'] == NOW*1000000 and rows[0]['result'] == outcome
        assert rows[0]['receipt']['original']['sha256'] == pages[0].digest
        assert h.missing_attempts == 0
        assert b'request-value-never-retained' not in b''.join(lines)
    finally:
        shell.close()
        shell.worker.join(2)
        stop_service(svc)


def test_idle_boot_uses_p3_allocation_function(monkeypatch):
    from types import SimpleNamespace

    from tests.test_async_settle_shadow_cohort import config
    from trusted_router.async_settle_shadow_evidence import uuid
    from trusted_router.services import async_settle_shadow_startup_harness as module
    assert module.uuid4 is uuid.uuid4
    expected = uuid.uuid4()
    monkeypatch.setattr(module, 'uuid4', lambda: expected)
    app = SimpleNamespace(state=SimpleNamespace())
    module.install(app, config())
    assert app.state.async_settle_shadow_startup.boot == str(expected)
    module.install(app, config())
    assert app.state.async_settle_shadow_startup.boot == str(expected)


async def test_bound_observation_read_once_and_no_request_target(monkeypatch):
    from tests.test_async_settle_shadow_observation import service as make_service
    from tests.test_async_settle_shadow_startup_harness import manifest
    from trusted_router.services.async_settle_shadow_startup_harness import dependency_operations
    svc = make_service()
    svc.settings.spanner_database_id = 'regional-store'
    m = manifest()
    calls = []
    native_read = svc.store.read
    def counted(kind, key, budget):
        calls.append((kind, key))
        return native_read(kind, key, budget)
    monkeypatch.setattr(svc.store, 'read', counted)
    operation = dependency_operations(svc, m)
    target = m['config']['dependencies']['router'][1]
    try:
        assert await operation(target, 1000000)
        with pytest.raises(ValueError):
            await operation(target, 1000000)
        with pytest.raises(ValueError):
            await operation({**target, 'resource': 'request-selected'}, 1000000)
        assert len(calls) == 1 and calls[0][1] == 'local/fleet'
    finally:
        stop_service(svc)


@pytest.mark.parametrize('partial', [False, True])
async def test_real_close_observed_endpoints(monkeypatch, partial):
    from tests.test_async_settle_shadow_observation import service as make_service
    from trusted_router.services.async_settle_shadow_startup_harness import bind_service
    svc = make_service()
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    h.monotonic_at_boot_us = round(svc.store.clock()*1e6)
    h.correlate(descriptor())
    bind_service(h, svc)
    if partial:
        def unavailable(*args):
            raise TimeoutError
        monkeypatch.setattr(svc.store, 'close_result', unavailable)
    try:
        await svc.close()
        spans = [json.loads(raw) for raw in lines if json.loads(raw)['kind'] == 'a3_shutdown_span']
        assert len(spans) == 1, h.missing
        assert spans[0]['complete'] is (not partial)
        assert spans[0]['outcome'] == 'graceful'
        assert spans[0]['clock_origin']['boot'] == svc.journal.boot
        assert all(pair[0] >= h.monotonic_at_boot_us for pair in spans[0]['spans'].values())
        assert any(key[0] == 'closure' for key in h.originals.buffers)
        assert any(key[0] == 'close_result' for key in h.originals.buffers)
    finally:
        stop_service(svc)


async def test_attempt_start_without_result_is_counted(monkeypatch):
    from trusted_router.services.async_settle_shadow_startup_harness import bind_service
    shell, svc, _ = installed()
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    bind_service(h, svc, shell)
    # Real journal has no room for a start PAGE; no result or projection is invented.
    day = next(iter(svc.journal.days))
    svc.journal.permits[day] = 0
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash='not-retained', model=ctx.authorization.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    try:
        shell.authorize_http(request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2')]), body,
            ctx.authorization, additions(), [endpoint()], typed=True, federated=False, replay=False)
        assert h.attempt_count == h.missing_attempts == 1
        assert not h.attempts and not lines
    finally:
        shell.close()
        shell.worker.join(2)
        stop_service(svc)


async def test_bound_slow_read_keeps_listener_available(monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace

    import httpx
    from fastapi import FastAPI

    from tests.test_async_settle_shadow_observation import service as make_service
    from tests.test_async_settle_shadow_startup_harness import manifest
    from trusted_router.services.async_settle_shadow_startup_harness import (
        dependency_operations,
        listener_started,
    )
    svc = make_service()
    svc.settings.spanner_database_id = 'regional-store'
    m = manifest()
    entered, release = threading.Event(), threading.Event()
    read = svc.store.read
    def delayed(*args):
        entered.set()
        assert release.wait(3)
        return read(*args)
    monkeypatch.setattr(svc.store, 'read', delayed)
    operation = dependency_operations(svc, m)
    h, _, _ = fake_harness()
    app = FastAPI()
    async def begin():
        await operation(m['config']['dependencies']['router'][1], 1000000)
    app.state.async_settle_shadow_startup = h
    app.state.async_settle_shadow_startup_begin = begin
    @app.get('/live')
    async def live():
        return {'live': True}
    try:
        listener_started(SimpleNamespace(app=app))
        assert await asyncio.to_thread(entered.wait, 1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            assert (await asyncio.wait_for(client.get('/live'), .2)).json() == {'live': True}
        assert not h.task.done()
    finally:
        release.set()
        if h.task is not None:
            await h.task
        stop_service(svc)


async def test_real_close_without_result_phase_emits_no_span(monkeypatch):
    from tests.test_async_settle_shadow_observation import service as make_service
    from trusted_router.services.async_settle_shadow_startup_harness import bind_service
    svc = make_service()
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    h.monotonic_at_boot_us = round(svc.store.clock()*1e6)
    h.correlate(descriptor())
    bind_service(h, svc)
    original = svc.store.flush
    def fail_final(pages, counter, closure, budget):
        if closure is not None:
            raise TimeoutError
        return original(pages, counter, closure, budget)
    monkeypatch.setattr(svc.store, 'flush', fail_final)
    try:
        await svc.close()
        assert svc.unclosed
        assert not [raw for raw in lines if json.loads(raw)['kind'] == 'a3_shutdown_span']
        assert 'P3ShutdownEndpoints' in h.missing
    finally:
        stop_service(svc)


async def test_request_cannot_choose_bound_dependency(monkeypatch):
    from tests.test_async_settle_shadow_startup_harness import manifest
    from trusted_router.services.async_settle_shadow_startup_harness import (
        bind_service,
        dependency_operations,
    )
    shell, svc, _ = installed()
    svc.settings.spanner_database_id = 'regional-store'
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    bind_service(h, svc, shell)
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash='request-value', model=ctx.authorization.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    try:
        shell.authorize_http(request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2'),
            ('X-Regional-Target', 'request-selected')]), body, ctx.authorization, additions(), [endpoint()],
            typed=True, federated=False, replay=False)
        shell.join()
        m = manifest()
        operation = dependency_operations(svc, m)
        assert await operation(m['config']['dependencies']['router'][1], 1000000)
        assert b'request-selected' not in b''.join(lines)
    finally:
        shell.close()
        shell.worker.join(2)
        stop_service(svc)


async def test_real_daily_close_keeps_ending_day():
    from tests.test_async_settle_shadow_observation import NOW_US
    from tests.test_async_settle_shadow_observation import service as make_service
    from trusted_router.services.async_settle_shadow_startup_harness import bind_service
    svc = make_service()
    wall = [NOW_US+86_400_000_000-100000]
    svc.store.wall = lambda: wall[0]/1e6
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    h.monotonic_at_boot_us = round(svc.store.clock()*1e6)
    h.correlate(descriptor())
    bind_service(h, svc)
    try:
        svc.tick(wall[0], publisher=False)
        wall[0] += 200000
        svc.flush(svc.store.clock()+4)
        records = [json.loads(raw) for raw in lines if json.loads(raw)['kind'] == 'a3_shutdown_span']
        assert len(records) == 1, h.missing
        assert records[0]['day'] == '2026-10-12' and records[0]['complete']
        assert records[0]['interval_end_us'] == NOW_US+86_400_000_000
        assert not svc.stopped.is_set()
        svc.flush(svc.store.clock()+4)
        assert len([raw for raw in lines if json.loads(raw)['kind'] == 'a3_shutdown_span']) == 1
    finally:
        stop_service(svc)


async def test_real_install_measures_nonzero_elapsed():
    from tests.test_async_settle_shadow_observation import NOW_US
    from tests.test_async_settle_shadow_observation import service as make_service
    from tests.test_async_settle_shadow_startup_harness import manifest
    from trusted_router.services.async_settle_shadow_startup_harness import bind_service
    svc = make_service()
    clock = [100.]
    svc.store.clock = svc.observer.clock = lambda: clock[0]
    svc.store.wall = lambda: NOW_US/1e6+clock[0]-100.
    svc.settings.async_settle_shadow_health_region = svc.settings.effective_serving_region
    h, _, _ = fake_harness()
    h.boot = svc.journal.boot
    h.boot_at_us = NOW_US-1
    h.now = lambda: NOW_US+500000
    h.manifest = manifest()
    bind_service(h, svc)
    try:
        svc.publish_cycle(NOW_US//2_000_000, NOW_US)
        read = svc.store.read_shadow_health()
        clock[0] += .249
        svc.observer.install_health(100., read)
        fields = await h.local_fields('observer_freshness', {})
        assert 248999 <= fields['install_elapsed_us'] <= 249001
        assert fields['installed_at_ns']-read['read_at_ns'] == fields['install_elapsed_us']*1000
    finally:
        stop_service(svc)


async def test_busy_attempt_recorder_preserves_admission(monkeypatch):
    from trusted_router.services.async_settle_shadow_startup_harness import bind_service
    shell, svc, _ = installed()
    h, lines, _ = fake_harness()
    h.boot = svc.journal.boot
    bind_service(h, svc, shell)
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    ctx = context()
    body = GatewayAuthorizeRequest(api_key_hash='not-retained', model=ctx.authorization.model_id,
        route_type='chat.completions', stream=False, requested_parameters=[])
    values = additions()
    try:
        with h.attempt_lock:
            shell.authorize_http(request([('X-TR-Settlement-Shadow-Policy', 'shadow-v2')]), body,
                ctx.authorization, values, [endpoint()], typed=True, federated=False, replay=False)
        shell.join()
        assert values.get('billing_shadow_binding')
        assert h.attempt_count == h.missing_attempts == h.attempt_overflow == 1
        assert not svc.port.lost.is_set() and not lines
    finally:
        shell.close()
        shell.worker.join(2)
        stop_service(svc)
