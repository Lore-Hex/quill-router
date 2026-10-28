"""Source-derived 7c2082f1 wire bodies, through real HTTP authorize and settle.

The ordinary decode seam adds a nonce; the configured lease-shadow seam also
adds an advisory echo. The old synthetic payload grid omitted that echo.
"""
from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.fixture_routes import bypass_catalog_caches
from tests.test_async_settle_shadow import NOW
from tests.test_async_settle_shadow_cohort_differential import registered_apps  # noqa: F401
from tests.test_async_settle_shadow_cohort_http import enclave_handoff
from tests.test_async_settle_shadow_cohort_runtime import installed
from tests.test_async_settle_shadow_http import pin_clocks
from tests.test_async_settle_ticket import endpoint
from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
from trusted_router import billing_snapshot as billing
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.async_settle_shadow_v2_evidence import EVENT
from trusted_router.catalog_data import Model
from trusted_router.detached_jws import b64encode, canonical
from trusted_router.routes.internal import gateway
from trusted_router.services import async_settle_shadow_cohort_runtime as cohort

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/async_settlement/enclave_authorize_probe.json').read_text())
CASES = FIXTURE['shapes']


@contextmanager
def probe_context(monkeypatch, app_factory, name, *, echo=True, changes=None,
                  ready=True, pin='shadow-v2', member=True, money=False, marker=True, workspace_id='ws-rpc'):
    pin_clocks(monkeypatch)
    monkeypatch.setattr('time.time_ns', lambda: NOW * 10**9)
    store, money_db, key = _seed_typed_gateway_store(workspace_id=workspace_id)
    payload = copy.deepcopy(CASES[name])
    payload['api_key_lookup_hash'] = key.lookup_hash
    if echo:
        payload.update(copy.deepcopy(FIXTURE['lease_shadow_additions']))
    if changes and changes.get('service_tier') == 'priority':
        payload['model'] = 'openai/gpt-5.6-luna'
    provider = payload['provider']['only'][0]
    e = replace(endpoint(), id=payload['model']+'@'+provider+'/prepaid',
                model_id=payload['model'], provider=provider, input_modalities=('text', 'image'))
    monkeypatch.setitem(gateway.MODELS, e.model_id, Model(id=e.model_id, provider=provider,
        name='Probe', context_length=1000000, prepaid_available=True))
    for identity, candidate in tuple(gateway.model_catalog.MODEL_ENDPOINTS.items()):
        if candidate.model_id == e.model_id:
            monkeypatch.delitem(gateway.model_catalog.MODEL_ENDPOINTS, identity)
    monkeypatch.setitem(gateway.model_catalog.MODEL_ENDPOINTS, e.id, e)
    bypass_catalog_caches(monkeypatch)
    payload.update(changes or {})
    shell, service, _ = installed()
    shell.startup_admission.ready = ready
    cfg = shell.settings
    cfg._async_settle_shadow_workspace_ids = frozenset({workspace_id if member else 'outside'})
    service.workspaces = cfg.async_settle_shadow_workspace_ids
    cfg.async_settle_enabled = money
    cfg.async_settle_shadow_policy = ''
    client = app_factory(cfg)
    cfg.async_settle_shadow_policy = pin
    client.app.state.async_settle = shell.async_runtime
    client.app.state.async_settle_shadow_cohort = shell
    def booking(identity, deadline):
        row = money_db.gateway_authorizations[identity]
        return Booking(row['finalized_cost_microdollars'], row['finalization_outcome'], row['settled'])
    shell.booking_store = SimpleNamespace(booking=booking)
    mapped = []
    mapping = cohort.map_requested_facts
    def capture(**kwargs):
        value = mapping(**kwargs)
        mapped.append(copy.deepcopy(value))  # producer clears its input on worker handoff
        return value
    monkeypatch.setattr(cohort, 'map_requested_facts', capture)
    def events():
        shell.join()
        return [EVENT.parse(s.body) for p in service.journal.pages for s in p.slots if s.body]
    def save():
        return {k: copy.deepcopy(v) for k, v in vars(money_db).items() if isinstance(v, (dict, list, set, int))}
    def restore(saved):
        for k, v in saved.items():
            setattr(money_db, k, copy.deepcopy(v))
        store._credit_shard_counts.invalidate(workspace_id)
    headers = {'X-TR-Settlement-Mode': 'async-v1'}
    if marker:
        headers['X-TR-Settlement-Shadow-Policy'] = 'shadow-v2'
    try:
        yield SimpleNamespace(client=client, shell=shell, service=service, cfg=cfg, payload=payload,
            headers=headers, key=key, provider=provider, mapped=mapped, events=events, save=save, restore=restore)
    finally:
        shell.close()
        shell.worker.join(2)
        client.close()


def authorize(env):
    response = env.client.post('/v1/internal/gateway/authorize', json=env.payload, headers=env.headers)
    assert response.status_code == 200, response.text
    return response


def without_observation(response):
    # The same response bytes after removing only additive observation members.
    raw = response.content
    for field in ('billing_shadow_policy', 'billing_shadow_binding', 'invocation_nonce',
                  'billing_snapshot', 'billing_snapshot_hash'):
        if field in response.json()['data']:
            fragment = b',' + canonical(field) + b':' + json.dumps(
                response.json()['data'][field], separators=(',', ':'), ensure_ascii=False).encode()
            assert raw.count(fragment) == 1
            raw = raw.replace(fragment, b'', 1)
    return raw


@pytest.mark.parametrize('name', CASES)
@pytest.mark.parametrize('echo', [False, True], ids=['ordinary', 'spend-lease-echo'])
def test_literal_enclave_probe_compares(monkeypatch, registered_apps, name, echo):  # noqa: F811
    with probe_context(monkeypatch, registered_apps, name, echo=echo) as env:
        assert_probe_compares(env)


def assert_probe_compares(env):
    before = env.save()
    env.cfg.async_settle_shadow_policy = ''
    baseline = authorize(env)
    baseline_money = env.save()
    env.restore(before)
    env.cfg.async_settle_shadow_policy = 'shadow-v2'
    response = authorize(env)
    events = env.events()
    data = response.json()['data']
    assert without_observation(response) == without_observation(baseline)
    for field in ('gateway_authorizations', 'reservations', 'typed', 'generation_records', 'settle_outbox'):
        assert env.save()[field] == baseline_money[field], field
    assert events[0]['eligibility'] == 'eligible', events[0]['diagnostic']
    assert env.mapped[0].reason is None and env.mapped[0].server_facts is not None
    assert env.mapped[0].complete_candidates and env.mapped[0].complete_supported_context
    assert data['billing_snapshot'] and data['billing_shadow_binding']
    assert data['invocation_nonce'] == env.payload['invocation_nonce']
    assert data['async_eligible'] is False
    route, stream = env.payload['route_type'], env.payload['stream']
    handoff = enclave_handoff(env.provider, route, stream)
    envelope = copy.deepcopy(handoff['object'])
    envelope.update(billing_snapshot=data['billing_snapshot'], billing_shadow_binding=data['billing_shadow_binding'])
    evaluated = billing.evaluate(billing.parse_snapshot(canonical(data['billing_snapshot'])),
        data['endpoint_id'], billing.RawUsage(**envelope['raw_usage']), billing.Eligibility(**envelope['observed']))
    envelope['terminal'].update(authorization_id=data['authorization_id'], generation_id=data['generation_id'],
        workspace_id='ws-rpc', key_id=env.key.hash, invocation_nonce=data['invocation_nonce'],
        selected_endpoint=data['endpoint_id'], snapshot_hash=data['billing_snapshot_hash'],
        charge_micro=evaluated.charge_micro, usage=evaluated.usage.model_dump())
    envelope['payload_hash'] = hashlib.sha256(canonical(envelope['terminal'])).hexdigest()
    terminal = {**handoff['body'], 'authorization_id': data['authorization_id'],
                'selected_endpoint': data['endpoint_id'], 'selected_model': env.payload['model']}
    headers = {'X-TR-Settlement-Shadow': b64encode(canonical(envelope))}
    authorized = env.save()
    outcome = env.client.post('/v1/internal/gateway/settle', json=terminal, headers=headers)
    assert outcome.status_code == 200, outcome.text
    events = env.events()
    settled = env.save()
    env.restore(authorized)
    env.cfg.async_settle_shadow_policy = ''
    baseline_terminal = env.client.post('/v1/internal/gateway/settle', json=terminal, headers=headers)
    assert (outcome.status_code, list(outcome.headers.multi_items()), outcome.content) == (
        baseline_terminal.status_code, list(baseline_terminal.headers.multi_items()), baseline_terminal.content)
    for field in ('gateway_authorizations', 'reservations', 'typed', 'generation_records', 'settle_outbox'):
        assert env.save()[field] == settled[field], field
    assert events[-1]['partition'] == 'A', events[-1]['diagnostic']
    counter = env.service.counters[events[-1]['authorization_day']]
    for field in ('requested_eligible', 'proof_compared', 'A'):
        assert counter[field] == 1, (field, counter)
    assert counter['B'] == counter['C'] == counter['requested_unknown'] == 0


@pytest.mark.parametrize('changes', [
    {'requested_parameters': None}, {'requested_parameters': ['tools']}, {'stream': None},
    {'input_modalities': ['text', 'image']}, {'unknown_feature': True},
    {'spend_lease_admission': {}}, {'models': ['openai/gpt-4.1-mini']},
    {'service_tier': 'priority'}, {'inference_receipt': True},
], ids=['missing-parameters', 'tools', 'missing-stream', 'image', 'unknown-extra',
        'consequential-admission', 'model-fallback', 'priority', 'receipt'])
def test_echo_does_not_supply_missing_facts_or_bypass_exclusions(monkeypatch, registered_apps, changes):  # noqa: F811
    with probe_context(monkeypatch, registered_apps, 'openai_chat_buffered', changes=changes) as env:
        data = authorize(env).json()['data']
        event = env.events()[0]
        assert event['eligibility'] == 'unknown'
        assert 'billing_shadow_binding' not in data
        assert env.service.counters[event['authorization_day']]['requested_eligible'] == 0


@pytest.mark.parametrize('options', [dict(pin=''), dict(member=False), dict(money=True), dict(marker=False)],
                         ids=['policy-off', 'outside-cohort', 'money-on', 'marker-absent'])
def test_echo_exception_does_not_change_v1_admission(monkeypatch, registered_apps, options):  # noqa: F811
    with probe_context(monkeypatch, registered_apps, 'openai_chat_buffered', **options) as env:
        data = authorize(env).json()['data']
        assert data['async_eligible'] is False
        assert 'billing_snapshot' not in data and 'billing_shadow_binding' not in data


def test_literal_body_before_startup_readiness_is_unknown_without_mapping(monkeypatch, registered_apps):  # noqa: F811
    with probe_context(monkeypatch, registered_apps, 'openai_chat_buffered', echo=False, ready=False) as env:
        data = authorize(env).json()['data']
        event = env.events()[0]
        assert not env.mapped
        assert event['eligibility'] == 'unknown' and event['facts'] is None and event['candidates'] is None
        assert event['diagnostic']['reason'] == 'missing_context'
        assert 'billing_shadow_binding' not in data
