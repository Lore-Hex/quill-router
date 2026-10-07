"""PR F1 complete state differentials and wire proofs (no shadow comparator)."""
# ruff: noqa: F811, F401 - shared fixtures
from __future__ import annotations

import copy
import json
import re
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_async_settle_handler import (
    NOW,
    ROOT,
    SUPPORTED,
    WIRE,
    call,
    env,
    prepare,
    row_for,
)
from tests.test_billing_snapshot import endpoint_from_candidate
from tests.test_settle_outbox_drain import _client, _typed_credit, _typed_key
from trusted_router import billing_snapshot as billing
from trusted_router.catalog_data import Model
from trusted_router.detached_jws import canonical
from trusted_router.routes.internal import gateway
from trusted_router.services import settle_outbox_apply
from trusted_router.services.settle_outbox_drain import drain_settle_outbox

STATE = ('typed', 'rows', 'reservations', 'gateway_authorizations', 'settle_outbox',
         'generation_records', 'operational_analytics_outbox', 'analytics_outbox',
         'reservation_idemp')


def save(db):
    return {name: copy.deepcopy(getattr(db, name)) for name in STATE}


def restore(db, state):
    for name, value in state.items():
        setattr(db, name, copy.deepcopy(value))


def catalog(monkeypatch, body):
    endpoints = {c['endpoint_id']: endpoint_from_candidate(c) for c in body['billing_snapshot']['candidates']}
    for endpoint in endpoints.values():
        monkeypatch.setitem(gateway.MODELS, endpoint.model_id, Model(
            id=endpoint.model_id, name='proof', provider=endpoint.provider,
            context_length=1_000_000, prepaid_available=True))
    monkeypatch.setattr(gateway, 'endpoint_for_id', endpoints.get)
    monkeypatch.setattr(settle_outbox_apply, 'endpoint_for_id', endpoints.get)
    return endpoints


def legacy_body(body):
    raw = body['raw_usage']
    return dict(authorization_id=body['terminal']['authorization_id'],
                selected_endpoint=body['terminal']['selected_endpoint'],
                actual_input_tokens=raw['input_tokens'], actual_output_tokens=raw['output_tokens'],
                cache_read_input_tokens=raw.get('cache_read_tokens', 0),
                cache_creation_input_tokens=raw.get('cache_creation_tokens', 0),
                reasoning_tokens=raw.get('reasoning_tokens', 0),
                route_type=body['terminal']['route_type'], streamed=body['terminal']['streamed'])


def wire_seed(env, kind):
    """Rename only seed identities; never normalize or rewrite handler reply bytes."""
    body, auth, key = prepare(env, kind=kind)
    mapping = {auth.id: 'auth-v1', auth.credit_reservation_id: 'res-v1', key.hash: 'key-v1',
               body['terminal']['generation_id']: WIRE['terminal']['generation_id']}
    def rename(value):
        if isinstance(value, str):
            for old, new in mapping.items():
                value = value.replace(old, new)
            return value
        if isinstance(value, dict):
            return {rename(k): rename(v) for k, v in value.items()}
        if isinstance(value, (tuple, list)):
            return type(value)(rename(v) for v in value)
        return value
    restore(env[1], rename(save(env[1])))
    body = copy.deepcopy(WIRE)
    if kind == 'refund':
        body['terminal'].update(terminal_kind='refund', charge_micro=0)
    return body


@pytest.mark.parametrize('kind', ['settle', 'refund'])
def test_snapshot_sync_wire(env, kind):
    body = wire_seed(env, kind)
    client = _client(env[3])
    client.app.state.async_settle = env[2]
    response = client.post('/v1/internal/gateway/' + kind, json=body,
                           headers={'X-TR-Settlement-Mode': 'sync'})
    assert response.status_code == 200, response.text
    name = 'snapshot_sync_v1.json' if kind == 'settle' else 'snapshot_sync_refund_v1.json'
    expected = json.loads((ROOT/name).read_bytes())
    assert response.json() == expected
    assert canonical(response.json()) == canonical(expected)
    # The JSONResponse's actual wire key order is a separate interoperability pin.
    assert response.content == json.dumps(expected, separators=(',', ':')).encode()


ERROR_CASES = ('invalid_snapshot', 'invalid_signature', 'charge_mismatch',
               'payload_conflict', 'storage_unavailable', 'not_found')


def error_envelope(env, monkeypatch, case):
    """Generate the literal through HTTP, freezing only response timing."""
    from trusted_router import gateway_timing
    from trusted_router.auth import require_inference_key
    from trusted_router.services import async_settle_handler
    from trusted_router.storage_errors import StoreUnavailable

    monkeypatch.setattr(gateway_timing, 'perf_counter', lambda: 1.0)
    body, _, _ = prepare(env)
    if case == 'invalid_snapshot':
        body['billing_snapshot'] = {}
    elif case == 'invalid_signature':
        body['settlement_ticket'] = 'invalid'
    elif case == 'charge_mismatch':
        body['terminal']['charge_micro'] += 1
    client = _client(env[3])
    client.app.state.async_settle = env[2]
    headers = {'X-TR-Settlement-Mode': 'async-v1'}
    path = '/v1/internal/gateway/settle'
    if case == 'payload_conflict':
        first = client.post(path, json=body, headers=headers)
        assert first.status_code == 202, first.text
        body['raw_usage']['input_tokens'] = 2
        body['terminal']['usage'].update(uncached_input_tokens=2, total_prompt_tokens=2)
    elif case == 'storage_unavailable':
        def unavailable():
            raise StoreUnavailable('F1 injected outage')
        monkeypatch.setattr(async_settle_handler, 'spanner_settle_outbox', unavailable)
    if case == 'not_found':
        client.app.dependency_overrides[require_inference_key] = lambda: SimpleNamespace(
            workspace=SimpleNamespace(id='ws-v1'), api_key=SimpleNamespace(hash='key-v1'))
        path = '/v1/settlements/auth-v1.settle'
        response = client.get(path)
    else:
        response = client.post(path, json=body, headers=headers)
    client.close()
    return dict(path=path, status=response.status_code,
                content_type=response.headers.get('content-type'),
                retry_after=response.headers.get('retry-after'), body_exact=response.text)


@pytest.mark.parametrize('case', ERROR_CASES)
def test_error_envelopes_real_http(env, monkeypatch, case):
    expected = json.loads((ROOT/'error_envelopes_v1.json').read_text())
    assert set(expected['cases']) == set(ERROR_CASES)
    assert error_envelope(env, monkeypatch, case) == expected['cases'][case]


@pytest.mark.parametrize('case', SUPPORTED, ids=lambda c: c['name'])
def test_four_path_billing_state(env, monkeypatch, case):
    run_four_paths(env, monkeypatch, case)


def run_four_paths(env, monkeypatch, case, scenario='ordinary'):
    body, auth, key = prepare(env, case)
    endpoints = catalog(monkeypatch, body)
    db, store = env[1], env[0]
    if scenario == 'catalog_change':
        for identity, endpoint in list(endpoints.items()):
            endpoints[identity] = replace(endpoint, price_tiers=(), prompt_price_microdollars_per_million_tokens=2_000_000,
                                          completion_price_microdollars_per_million_tokens=3_000_000)
    elif scenario == 'endpoint_removal':
        endpoints.clear()
    elif scenario == 'debt':
        _typed_credit(db, 'ws-v1')['total_credits'] = 0
        _typed_credit(db, 'ws-v1')['total_usage'] = 7
    elif scenario == 'deleted_key':
        db.typed['tr_key_limit'].clear()
        db.rows.pop(('api_key', key.hash), None)
    elif scenario == 'window_rollover':
        from datetime import UTC, datetime
        for window in ('day', 'week', 'month'):
            _typed_key(db, key.hash)[window+'_start'] = datetime(2000, 1, 1, tzinfo=UTC)
            _typed_key(db, key.hash)[window+'_usage'] = 99
    initial = save(db)
    client = _client(env[3])
    client.app.state.async_settle = env[2]
    outputs = []
    for path in ('legacy', 'async', 'duplicate', 'snapshot_sync'):
        restore(db, initial)
        if path == 'legacy':
            reply = client.post('/v1/internal/gateway/settle', json=legacy_body(body))
            if scenario == 'endpoint_removal':
                assert reply.status_code == 400, reply.text
                assert save(db) == initial
                outputs.append({'cost': None})
                continue
            assert reply.status_code == 200, reply.text
        elif path == 'snapshot_sync':
            reply = call(env, body, synchronous=True)
            assert reply.status_code == 200
            assert json.loads(reply.body)['data']['acceptance']['payload_hash'] == billing.canonical_hash(
                billing.TerminalEnvelope(**body['terminal']))
        else:
            assert call(env, body).status_code == 202
            if path == 'duplicate':
                before = save(db)
                assert json.loads(call(env, body).body)['data']['acceptance']['status'] == 'duplicate'
                assert save(db) == before
            assert drain_settle_outbox(10)['outcomes'] == {'settled_now': 1}
        settled = store.get_gateway_authorization(auth.id)
        generation = store.get_generation(settled.finalized_generation_id)
        reservation = db.reservations[auth.credit_reservation_id]
        expected = case['expected_charge_micro']
        if path == 'legacy' and scenario == 'catalog_change':
            expected = 5
        if path == 'legacy' and scenario == 'endpoint_removal':
            expected = 0
        key_state = _typed_key(db, key.hash) if scenario != 'deleted_key' else {'usage': None, 'reserved': None}
        fields = dict(credit=_typed_credit(db, 'ws-v1')['total_usage'],
                      key=key_state['usage'],
                      credit_hold=_typed_credit(db, 'ws-v1')['reserved'],
                      key_hold=key_state['reserved'],
                      actual=reservation['actual_micro'], reservation_settled=reservation['settled'],
                      settled=settled.settled, cost=settled.finalized_cost_microdollars,
                      outcome=settled.finalization_outcome, generation_id=settled.finalized_generation_id,
                      generation_amount=generation.total_cost_microdollars)
        assert fields == dict(credit=expected + (7 if scenario == 'debt' else 0), key=expected if scenario != 'deleted_key' else None, credit_hold=0, key_hold=0 if scenario != 'deleted_key' else None,
                             actual=expected, reservation_settled=True, settled=True, cost=expected,
                             outcome='settled', generation_id=body['terminal']['generation_id'],
                             generation_amount=expected)
        outputs.append(fields)
        row = db.settle_outbox.get((auth.id, 'settle'))
        if path == 'snapshot_sync':
            assert row is None
        else:
            assert row['status'] == 'done' and row['settle_body'] is None
            assert row['terminal_at'] is not None
            if path in ('async', 'duplicate'):
                assert row['payload_hash'] == billing.canonical_hash(billing.TerminalEnvelope(**body['terminal']))
                assert row['snapshot_hash'] == body['terminal']['snapshot_hash']
            else:
                assert row.get('payload_hash') is None and row.get('snapshot_hash') is None
        if path != 'snapshot_sync':
            assert reservation['terminal_at'] is not None
        if scenario == 'window_rollover':
            assert [key_state[window+'_usage'] for window in ('day', 'week', 'month')] == [expected]*3
        if scenario == 'debt':
            assert _typed_credit(db, 'ws-v1')['total_credits'] - fields['credit'] < 0
    if scenario in ('catalog_change', 'endpoint_removal'):
        assert outputs[1:] == [outputs[1]]*3
        assert outputs[0]['cost'] == (5 if scenario == 'catalog_change' else None)
        assert outputs[1]['cost'] == 2
    else:
        assert outputs == [outputs[0]] * 4


@pytest.mark.parametrize('scenario', ['catalog_change', 'endpoint_removal', 'debt', 'deleted_key', 'window_rollover'])
def test_four_path_scenario_axes(env, monkeypatch, scenario):
    run_four_paths(env, monkeypatch, SUPPORTED[0], scenario)


@pytest.mark.xfail(strict=True, reason='F1-001: fresh snapshot-sync defers retention without a durable repair row')
@pytest.mark.parametrize('kind', ['settle', 'refund'])
@pytest.mark.parametrize('record', ['reservation', 'authorization'])
def test_fresh_snapshot_sync_completes_retention(env, kind, record):
    body, auth, _ = prepare(env, kind=kind)
    assert call(env, body, synchronous=True).status_code == 200
    assert env[1].settle_outbox == {}
    assert env[1].reservations[auth.credit_reservation_id]['settled']
    terminal = (env[1].reservations[auth.credit_reservation_id] if record == 'reservation'
                else env[1].gateway_authorizations[auth.id])
    assert terminal['terminal_at'] is not None


LITERALS = [
    ('authorize_v1_builder.json', ('claims',)),
    ('authorize_v1_builder.json', ('response',)),
    ('request_v1.json', ()), ('pending_v1.json', ()),
    ('snapshot_sync_v1.json', ()), ('snapshot_sync_refund_v1.json', ()),
    ('pending_v1.json', ('data', 'trusted_router_settlement')), ('sync_required_v1.json', ('drain_unhealthy',)),
    ('error_envelopes_v1.json', ()),
]


def test_all_section_three_json_literals():
    source = (ROOT.parents[2]/'docs/design/async-settle-outbox-v1.md').read_text()
    section = source.split('## 3. Wire contract', 1)[1].split('## 4. Router request path', 1)[0]
    actual = [json.loads(raw) for raw in re.findall(r'```json\n(.*?)\n```', section, re.S)]
    expected = []
    for name, keys in LITERALS:
        value = json.loads((ROOT/name).read_text())
        for key in keys:
            value = value[key]
        expected.append({'trusted_router_settlement': value} if keys == ('data', 'trusted_router_settlement') else value)
    expected.append({name: json.loads((ROOT/f'status_{name}_v1.json').read_text())
                     for name in ('pending', 'settled', 'refunded')})
    assert actual == expected
    assert [canonical(v) for v in actual] == [canonical(v) for v in expected]
