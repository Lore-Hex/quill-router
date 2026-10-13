"""P4a handlers with P2 compared byte-template transport and real money storage."""
from __future__ import annotations

import copy
import json
from dataclasses import replace
from itertools import product
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_async_settle_shadow import NOW
from tests.test_async_settle_shadow_cohort import fixture
from tests.test_async_settle_shadow_cohort_runtime import installed
from tests.test_async_settle_shadow_http import pin_clocks
from tests.test_async_settle_ticket import endpoint, runtime
from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
from tests.test_settle_outbox_drain import _client
from trusted_router import billing_snapshot as billing
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_v2_evidence import EVENT
from trusted_router.catalog_data import Model
from trusted_router.detached_jws import b64encode, canonical
from trusted_router.routes.internal import gateway


def enclave_handoff(adapter, route, streamed):
    path = Path(__file__).parent/'fixtures/async_settlement/p4a_handoffs.json'
    shape = json.loads(path.read_bytes())['shapes'][f'{adapter}-{route}-{str(streamed).lower()}']
    assert shape['off_on_bytes_equal'] is True
    assert shape['path'] == '/internal/gateway/settle'
    assert bool(shape['stream']) is streamed
    return shape


def probe(monkeypatch, adapter, route, streamed, *, marker=True, kind='settle', preinvoke=False, changes=None, dormant=None):
    pin_clocks(monkeypatch)
    monkeypatch.setattr('time.time_ns', lambda: NOW*10**9)
    store, money_db, key = _seed_typed_gateway_store()
    e = endpoint()
    if changes and changes.get('service_tier') == 'priority':
        e = replace(e, id='openai/gpt-5.6-luna@openai/prepaid', model_id='openai/gpt-5.6-luna')
    if adapter == 'anthropic':
        e = replace(e, id='anthropic/billing-v1@anthropic/prepaid', model_id='anthropic/billing-v1', provider='anthropic')
    model = Model(id=e.model_id, provider=e.provider, name='Probe', context_length=1000000, prepaid_available=True)
    monkeypatch.setitem(gateway.MODELS, model.id, model)
    monkeypatch.setitem(gateway.model_catalog.MODEL_ENDPOINTS, e.id, e)
    from tests.fixture_routes import clear_catalog_caches
    clear_catalog_caches()
    shell, service, _ = installed()
    cfg = shell.settings
    cfg._async_settle_shadow_workspace_ids = frozenset({'ws-rpc'})
    service.workspaces = cfg.async_settle_shadow_workspace_ids
    def booking(identity, deadline):
        row = money_db.gateway_authorizations[identity]
        return Booking(row["finalized_cost_microdollars"], row["finalization_outcome"], row["settled"])
    shell.booking_store = SimpleNamespace(booking=booking)
    client = _client(cfg)
    client.app.state.async_settle = runtime()
    client.app.state.async_settle_shadow_cohort = shell
    if dormant == 'money':
        cfg.async_settle_enabled = True
    if dormant == 'nonmember':
        cfg._async_settle_shadow_workspace_ids = frozenset({'other'})
    active_pin = '' if dormant == 'empty' else 'shadow-v2'
    payload = dict(api_key_hash=key.hash, model=e.model_id, route_type=route, stream=streamed,
        requested_parameters=['max_tokens'], estimated_input_tokens=1, max_tokens=2, invocation_nonce='nonce-v1')
    payload.update(changes or {})
    eligible = marker and not changes and dormant is None
    headers = {'X-TR-Settlement-Mode': 'async-v1'}
    if marker:
        headers['X-TR-Settlement-Shadow-Policy'] = 'shadow-v2'
    def saved_money():
        return {k: copy.deepcopy(v) for k, v in vars(money_db).items() if isinstance(v, (dict, list, set, int))}
    def restore_money(saved):
        for k, v in saved.items():
            setattr(money_db, k, copy.deepcopy(v))
        store._credit_shard_counts.invalidate('ws-rpc')
    try:
        before = saved_money()
        cfg.async_settle_shadow_policy = ''
        baseline_authorize = client.post('/v1/internal/gateway/authorize', json=payload, headers=headers)
        baseline_authorized = saved_money()
        restore_money(before)
        cfg.async_settle_shadow_policy = active_pin
        response = client.post('/v1/internal/gateway/authorize', json=payload, headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()['data']
        stripped = response.json()
        for field in ('billing_shadow_policy', 'billing_shadow_binding', 'invocation_nonce'):
            stripped['data'].pop(field, None)
        assert stripped == baseline_authorize.json(), [k for k in stripped['data'] if stripped['data'][k] != baseline_authorize.json()['data'].get(k)]
        stripped_bytes = response.content
        for field in ('billing_shadow_policy', 'billing_shadow_binding', 'invocation_nonce'):
            if field in data:
                fragment = (','+json.dumps(field)+':'+json.dumps(data[field])).encode()
                assert stripped_bytes.count(fragment) == 1
                stripped_bytes = stripped_bytes.replace(fragment, b'', 1)
        assert stripped_bytes == baseline_authorize.content
        for name in ('gateway_authorizations', 'reservations', 'typed'):
            assert getattr(money_db, name) == baseline_authorized[name], name
        authorized = saved_money()
        events = [EVENT.parse(s.body) for p in service.journal.pages for s in p.slots if s.body]
        assert len(events) == (0 if dormant else 1), events
        if eligible:
            assert data.get('billing_shadow_policy') == 'shadow-v2', events[0].get('diagnostic')
            assert data['invocation_nonce'] == 'nonce-v1'
            captured = enclave_handoff(adapter, route, streamed) if kind == 'settle' and not preinvoke else None
            value = copy.deepcopy(captured['object'] if captured else fixture('terminal_compared_full')[0]['object'])
            value['billing_shadow_binding'] = data['billing_shadow_binding']
            if captured:
                assert value['billing_snapshot'] == data['billing_snapshot']
                assert (value['terminal']['route_type'], value['terminal']['streamed']) == (route, streamed)
            value['billing_snapshot'] = data['billing_snapshot']
            value['observed'].update(route_type=route, streamed=streamed)
            snap = billing.parse_snapshot(canonical(data['billing_snapshot']))
            raw = billing.RawUsage(**value['raw_usage'])
            observed = billing.Eligibility(**value['observed'])
            evaluated = billing.evaluate(snap, data['endpoint_id'], raw, observed)
            value['terminal'].update(authorization_id=data['authorization_id'], generation_id=data['generation_id'],
                workspace_id='ws-rpc', key_id=key.hash, invocation_nonce='nonce-v1',
                route_type=route, streamed=streamed, selected_endpoint=data['endpoint_id'], terminal_kind=kind,
                snapshot_hash=data['billing_snapshot_hash'])
            if captured:
                assert value['terminal']['charge_micro'] == evaluated.charge_micro
                assert value['terminal']['usage'] == evaluated.usage.model_dump()
            else:
                value['terminal'].update(charge_micro=evaluated.charge_micro if kind == 'settle' else 0,
                                         usage=evaluated.usage.model_dump())
            import hashlib
            value['payload_hash'] = hashlib.sha256(canonical(value['terminal'])).hexdigest()
            if preinvoke:
                value.update(raw_usage=None, terminal=None, payload_hash=None, go_error='usage_missing')
            terminal_headers = {'X-TR-Settlement-Shadow': b64encode(canonical(value))}
        else:
            assert 'billing_shadow_policy' not in data and 'billing_shadow_binding' not in data
            if not dormant:
                assert events[0]['diagnostic']['reason'] is not None
            if not marker and not dormant:
                assert events[0]['diagnostic']['reason'] == 'header_absent'
            terminal_headers = {}
        terminal = dict(authorization_id=data['authorization_id'], selected_endpoint=data['endpoint_id'],
            actual_input_tokens=1, actual_output_tokens=1, route_type=payload['route_type'], streamed=streamed)
        if eligible and kind == 'settle' and not preinvoke:
            assert captured is not None
            terminal = {**captured['body'], 'authorization_id': data['authorization_id']}
        if preinvoke:
            terminal = dict(authorization_id=data['authorization_id'], error_status=502, streamed=True)
        outcome = client.post('/v1/internal/gateway/'+kind, json=terminal, headers=terminal_headers)
        assert outcome.status_code == 200, outcome.text
        shell.executor.shutdown(wait=True)
        actual_terminal = saved_money()
        restore_money(authorized)
        cfg.async_settle_shadow_policy = ''
        baseline_terminal = client.post('/v1/internal/gateway/'+kind, json=terminal, headers=terminal_headers)
        assert (outcome.status_code, list(outcome.headers.multi_items()), outcome.content) == (
            baseline_terminal.status_code, list(baseline_terminal.headers.multi_items()), baseline_terminal.content)
        for name in ('gateway_authorizations', 'reservations', 'typed'):
            assert getattr(money_db, name) == actual_terminal[name], name
        cfg.async_settle_shadow_policy = active_pin
        events = [EVENT.parse(s.body) for p in service.journal.pages for s in p.slots if s.body]
        if dormant:
            assert events == []
            assert all(c['authorize_attempts'] == c['settle_attempts'] == c['refund_attempts'] == 0 for c in service.counters.values())
            return events
        assert len(events) == 2
        event = events[-1]
        assert event['phase'] == kind
        assert event['partition'] == ('A' if eligible and not preinvoke else 'C'), event['diagnostic']
        if eligible and not preinvoke:
            assert event['dimensions'] == dict(adapter=adapter, route_type=route, streamed=streamed)
            assert event['prediction'] is not None
            assert sum(len(v) for v in service.journal.batches.values()) == 1
            counter = service.counters[event['authorization_day']]
            assert (counter['A'], counter['B'], counter['C'], counter['eligible_demand']) == (1, 0, 0, 1)
            assert counter[kind+'_attempts'] == 1
            assert counter['admission_observer']['prediction_'+event['prediction']['prediction']] == 1
        return events
    finally:
        shell.close()
        client.close()


@pytest.mark.parametrize('adapter,route,streamed', list(product(['openai', 'anthropic'], ['chat.completions', 'responses'], [False, True])))
def test_eight_probe_shapes_authorize_and_terminal(monkeypatch, adapter, route, streamed):
    probe(monkeypatch, adapter, route, streamed)


def test_old_enclave_header_absent_and_refund(monkeypatch):
    probe(monkeypatch, 'openai', 'chat.completions', False, marker=False, kind='refund')


def test_compared_refund_uses_confirmed_zero_booking(monkeypatch):
    probe(monkeypatch, 'openai', 'responses', True, kind='refund')


def test_preinvoke_refund_is_one_c(monkeypatch):
    probe(monkeypatch, 'openai', 'responses', False, kind='refund', preinvoke=True)


@pytest.mark.parametrize('changes', [dict(service_tier='priority'), dict(inference_receipt=True),
    dict(requested_parameters=['unknown']), dict(stream=None), dict(route_type='embeddings')])
def test_noneligible_authorize_no_fields_and_c_terminal(monkeypatch, changes):
    probe(monkeypatch, 'openai', 'chat.completions', False, changes=changes)


@pytest.mark.parametrize('dormant', ['empty', 'nonmember', 'money'])
def test_installed_worker_dormant_http_bytes_money_and_counts(monkeypatch, dormant):
    probe(monkeypatch, 'openai', 'chat.completions', False, dormant=dormant)


def test_lifecycle_installs_worker_only_in_existing_v2_branch(monkeypatch):
    from tests.test_async_settle_shadow_cohort import config
    from trusted_router.main import create_app
    from trusted_router.services import async_settle_shadow_cohort_runtime as module
    _seed_typed_gateway_store()
    original = module.install
    calls = []
    def install(app, settings, backend):
        calls.append(1)
        original(app, settings, backend)
    monkeypatch.setattr(module, 'install', install)
    for pin, workspaces, money in [('', 'ws-rpc', False), ('shadow-v2', '', False),
                                   ('shadow-v2', 'ws-rpc', True), ('shadow-v2', 'ws-rpc', False)]:
        cfg = config(async_settle_shadow_policy=pin, async_settle_shadow_workspaces=workspaces,
                     async_settle_enabled=money)
        app = create_app(cfg, configure_store_arg=False, init_observability=False)
        active = pin == 'shadow-v2' and bool(workspaces) and not money
        assert hasattr(app.state, 'async_settle_shadow_cohort') is active
        if active:
            shell = app.state.async_settle_shadow_cohort
            assert shell.start in app.router.on_startup
            assert shell.close in app.router.on_shutdown
            shell.close()
    assert calls == [1]
