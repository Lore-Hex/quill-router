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
    from trusted_router.main import _v2_install_hook
    app = SimpleNamespace(state=SimpleNamespace())
    _v2_install_hook(app, config(), object(), 'ws-v1')
    assert app.state.async_settle_shadow_startup_missing == 'JournalBootAllocation'
