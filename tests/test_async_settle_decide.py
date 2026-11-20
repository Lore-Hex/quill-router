"""Decide admission, wire identity, and frozen settlement parity."""
from __future__ import annotations

import copy
import json
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

from tests.test_async_settle_admission import request
from tests.test_async_settle_handler import NOW, WIRE, _drain_async, call, prepare
from tests.test_async_settle_handler import env as env  # noqa: F401
from tests.test_async_settle_proof import (
    catalog,
    legacy_body,
    restore,
    retention_clock,
    run_four_paths,
    save,
)
from tests.test_async_settle_ticket import (
    FIXTURE,
    authorization,
    endpoint,
    runtime,
    settings,
    signer,
)
from tests.test_gateway_authorize_spanner_operations import _seed_typed_gateway_store
from tests.test_settle_outbox_drain import _client
from trusted_router import billing_snapshot as billing
from trusted_router.async_settle_ticket import TicketClaims, verify_lookup_ticket
from trusted_router.catalog import MODEL_ENDPOINTS, effective_endpoint
from trusted_router.config import Settings
from trusted_router.detached_jws import canonical
from trusted_router.schemas import GatewayAuthorizeRequest
from trusted_router.services import async_settle, async_settle_handler
from trusted_router.types import UsageType

FIXTURE_PATH = Path(__file__).parent / 'fixtures/async_settlement/decide_v1.json'
HOSTED = next(e for e in MODEL_ENDPOINTS.values() if e.model_id == 'typesafe-ai/jev')


def hosted_body(**changes):
    return GatewayAuthorizeRequest(**dict(
        api_key_lookup_hash='lookup-v1', model=HOSTED.model_id, estimated_input_tokens=1234,
        max_output_tokens=1, region='us-central1', route_type='decide',
        idempotency_key='decide-v1', user='fixture-user', session_id='fixture-session',
        **changes,
    ))


def answer(body=None, *, enabled=True, optin=True, auth=None, routes=None, **changes):
    return async_settle.authorize_additions(
        request=request(runtime(), optin), body=body or hosted_body(),
        authorization=auth or replace(authorization(), model_id=HOSTED.model_id,
                                     requested_model_id=HOSTED.model_id, provider=HOSTED.provider),
        endpoints=routes or [HOSTED], settings=settings(async_settle_decide_route=enabled),
        **{'typed': True, 'federated': False, 'replay': False,
           'effective_at': datetime.fromtimestamp(NOW, UTC), **changes},
    )


def decide_case(native=False):
    ep = endpoint() if native else HOSTED
    snapshot = billing.build_snapshot([effective_endpoint(ep, at=datetime.fromtimestamp(NOW, UTC))], billing.Eligibility(route_type='decide'))
    raw = billing.RawUsage(input_tokens=1234, output_tokens=7 if native else 0)
    observed = billing.Eligibility(route_type='decide', streamed=False)
    result = billing.evaluate(snapshot, ep.id, raw, observed)
    return dict(name='native' if native else 'hosted', snapshot=snapshot.model_dump(mode='json'),
                context=observed.model_dump(), raw_usage=raw.model_dump(), selected_endpoint=ep.id,
                expected_normalized_usage=result.usage.model_dump(), expected_charge_micro=result.charge_micro)


def test_config_default_and_environment(monkeypatch):
    monkeypatch.delenv('TR_ASYNC_SETTLE_DECIDE_ROUTE', raising=False)
    assert Settings(environment='test').async_settle_decide_route is False
    for text, expected in [('true', True), ('false', False)]:
        monkeypatch.setenv('TR_ASYNC_SETTLE_DECIDE_ROUTE', text)
        assert Settings(environment='test').async_settle_decide_route is expected


@pytest.mark.parametrize('optin', [False, True])
def test_decide_dormancy_is_the_unsupported_route_answer(optin):
    unsupported = hosted_body().model_copy(update={'route_type': 'embeddings'})
    assert answer(enabled=True)['async_eligible'] is True
    assert canonical(answer(enabled=False, optin=optin)) == canonical(answer(unsupported, optin=optin))
    assert answer(enabled=False, optin=optin) == ({'async_eligible': False} if optin else {})


@pytest.mark.parametrize('stream', [None, False, True])
def test_decide_authorize_ticket_is_nonstreamed(monkeypatch, stream):
    monkeypatch.setattr(async_settle.time, 'time', lambda: NOW)
    body = hosted_body() if stream is None else hosted_body(stream=stream, requested_parameters=['response_format'])
    if stream is None:
        reply = answer(body)
    else:
        body.model = endpoint().model_id
        reply = answer(body, auth=authorization(), routes=[endpoint()])
    assert reply['async_eligible'] is True
    claims = verify_lookup_ticket(reply['settlement_ticket'], [signer().trusted], NOW)
    assert claims.route_type == 'decide' and claims.streamed is False
    assert claims.snapshot_hash == reply['billing_snapshot_hash']
    assert 'settlement_status_url' in reply
    if stream is None:
        assert not {'stream', 'requested_parameters'} & body.model_fields_set


@pytest.mark.parametrize('change', ['usage', 'tier', 'receipt', 'markup', 'program', 'modalities',
                                    'parameter', 'replay', 'extras', 'federated', 'untyped', 'request_fee'])
def test_decide_retains_exclusions(change):
    assert answer()['async_eligible'] is True
    body, auth, routes, options = hosted_body(), authorization(), [HOSTED], {}
    if change == 'usage':
        auth.usage_type = UsageType.BYOK
    elif change == 'tier':
        body.service_tier = 'priority'
    elif change == 'receipt':
        body.inference_receipt = True
    elif change == 'markup':
        auth.app_markup_basis_points = 1
    elif change == 'program':
        body.model = 'trustedrouter/auto'
    elif change == 'modalities':
        body.input_modalities = ['text']
    elif change == 'parameter':
        body.requested_parameters = ['decision_host']
    elif change == 'replay':
        options['replay'] = True
    elif change == 'extras':
        body = hosted_body(decision_host='internal')
    elif change == 'federated':
        options['federated'] = True
    elif change == 'untyped':
        options['typed'] = False
    elif change == 'request_fee':
        routes = [replace(HOSTED, request_price_microdollars=1)]
    assert answer(body, auth=auth, routes=routes, **options) == {'async_eligible': False}


@pytest.mark.parametrize('route', ['chat.completions', 'responses'])
@pytest.mark.parametrize('stream', [False, True])
def test_chat_ticket_bytes_ignore_decide_setting(monkeypatch, route, stream):
    monkeypatch.setattr(async_settle.time, 'time', lambda: NOW)
    body = hosted_body().model_copy(update={'model': endpoint().model_id, 'route_type': route, 'stream': stream})
    assert answer()['async_eligible'] is True
    replies = [answer(body, enabled=enabled, auth=authorization(), routes=[endpoint()]) for enabled in (False, True)]
    assert replies[0]['async_eligible'] is True
    assert canonical(replies[0]) == canonical(replies[1])


@pytest.mark.parametrize('route,stream', [('decide', True), ('embeddings', False)])
def test_ticket_and_envelope_reject_invalid_route_stream(route, stream):
    assert TicketClaims.model_validate({**FIXTURE['claims'], 'route_type': 'decide'}).route_type == 'decide'
    assert billing.TerminalEnvelope.model_validate({**WIRE['terminal'], 'route_type': 'decide'}).route_type == 'decide'
    claims = {**FIXTURE['claims'], 'route_type': route, 'streamed': stream}
    terminal = {**WIRE['terminal'], 'route_type': route, 'streamed': stream}
    with pytest.raises(ValueError):
        TicketClaims.model_validate(claims)
    with pytest.raises(ValueError):
        billing.TerminalEnvelope.model_validate(terminal)


@pytest.mark.parametrize('enabled', [False, True])
@pytest.mark.parametrize('optin', [False, True])
def test_decide_real_authorize_route(enabled, optin):
    """A hosted decide authorize through the route itself."""
    store, _, key = _seed_typed_gateway_store()
    config = settings(async_settle_decide_route=enabled, async_settle_pilot_workspaces='ws-rpc',
                      regions='us-central1', primary_region='us-central1', serving_region='us-central1')
    client = _client(config)
    client.app.state.async_settle = runtime()
    try:
        body = {'api_key_hash': key.hash, 'model': HOSTED.model_id, 'estimated_input_tokens': 1234,
                'max_output_tokens': 1, 'region': 'us-central1', 'route_type': 'decide',
                'idempotency_key': f'decide-route-{enabled}-{optin}', 'invocation_nonce': 'nonce-route'}
        response = client.post('/v1/internal/gateway/authorize', json=body,
                               headers={'X-TR-Settlement-Mode': 'async-v1'} if optin else {})
        assert response.status_code == 200, response.text
        data = response.json()['data']
        stored = store.get_gateway_authorization(data['authorization_id'])
        assert stored.model_id == HOSTED.model_id and data['endpoint_id'] == HOSTED.id
        added = {name for name in ('async_eligible', 'billing_snapshot', 'billing_snapshot_hash',
                                   'settlement_ticket', 'settlement_status_url') if name in data}
        if not optin:
            assert added == set()
        elif not enabled:
            # The answer every release before this route gave.
            assert added == {'async_eligible'} and data['async_eligible'] is False
        else:
            assert added == {'async_eligible', 'billing_snapshot', 'billing_snapshot_hash',
                             'settlement_ticket', 'settlement_status_url'}
            assert data['async_eligible'] is True
            claims = verify_lookup_ticket(data['settlement_ticket'], [signer().trusted], int(time.time()) + 5)
            assert (claims.route_type, claims.streamed) == ('decide', False)
            assert claims.authorization_id == data['authorization_id']
            snapshot = billing.parse_snapshot(json.dumps(data['billing_snapshot']))
            assert [c.endpoint_id for c in snapshot.candidates] == [HOSTED.id]
    finally:
        client.close()


@pytest.mark.parametrize('streamed', [False, True])
def test_eligibility_refuses_a_streamed_decide(streamed):
    """The rule itself, not only the ticket and envelope validators in front of it."""
    facts = billing.Eligibility(route_type='decide', streamed=streamed)
    assert billing.exclusion(facts) == ('unsupported_route' if streamed else None)
    # A retained shadow caller excludes the route whatever its stream.
    assert billing.exclusion(facts, allow_decide=False) == 'unsupported_route'
    # The other routes do not read the stream.
    assert billing.exclusion(facts.model_copy(update={'route_type': 'chat.completions'})) is None
    if streamed:
        with pytest.raises(ValueError, match='unsupported_route'):
            billing.require_eligible(facts)
    else:
        billing.require_eligible(facts)


@pytest.mark.parametrize('enabled', [True, False])
def test_decide_refund(env, enabled):
    env[3].async_settle_decide_route = True
    body, auth, _ = prepare(env, decide_case(), kind='refund')
    assert body['terminal']['route_type'] == 'decide' and body['terminal']['charge_micro'] == 0
    env[3].async_settle_decide_route = enabled
    if enabled:
        # A nonzero refund is not a decide refund: refused before any row exists.
        charged = copy.deepcopy(body)
        charged['terminal']['charge_micro'] = 1
        with pytest.raises(HTTPException) as refused:
            call(env, charged)
        assert refused.value.status_code == 400 and not env[1].settle_outbox
    reply = call(env, body)
    if not enabled:
        assert reply.status_code == 200
        assert json.loads(reply.body)['data']['reason'] == 'unsupported_cohort'
        assert not env[1].settle_outbox
        return
    assert reply.status_code == 202
    assert _drain_async()['outcomes'] == {'settled_now': 1}
    assert env[1].settle_outbox[(auth.id, 'refund')]['actual_cost_micro'] == 0
    stored = env[0].get_gateway_authorization(auth.id)
    assert (stored.settled, stored.finalization_outcome, stored.finalized_cost_microdollars) == (True, 'refunded', 0)


@pytest.mark.parametrize('ticket_route,envelope_route', [('decide', 'chat.completions'), ('chat.completions', 'decide')])
def test_ticket_binds_route_in_both_directions(env, ticket_route, envelope_route):
    env[3].async_settle_decide_route = True
    case = decide_case()
    case['context']['route_type'] = ticket_route
    body, _, _ = prepare(env, case)
    body['terminal']['route_type'] = body['observed']['route_type'] = envelope_route
    with pytest.raises(HTTPException) as exc:
        call(env, body)
    assert exc.value.status_code == 401


@pytest.mark.parametrize('change,code,reason', [('valid', 202, None), ('amount', 409, None),
    ('cohort', 200, 'unsupported_cohort'), ('setting', 200, 'unsupported_cohort')])
def test_decide_handler(env, change, code, reason):
    env[3].async_settle_decide_route = True
    body, _, _ = prepare(env, decide_case())
    if change == 'amount':
        body['terminal']['charge_micro'] += 1
    elif change == 'cohort':
        body['observed']['tool_cost'] = True
    elif change == 'setting':
        env[3].async_settle_decide_route = False
    if code == 409:
        with pytest.raises(HTTPException) as exc:
            call(env, body)
        assert exc.value.status_code == code
    else:
        reply = call(env, body)
        assert reply.status_code == code
        if reason:
            assert json.loads(reply.body)['data']['reason'] == reason
    if code != 202:
        assert not env[1].settle_outbox


def test_decide_duplicate_conflict_and_drain_after_setting_off(env):
    env[3].async_settle_decide_route = True
    body, auth, _ = prepare(env, decide_case())
    assert call(env, body).status_code == 202
    env[3].async_settle_decide_route = False
    assert json.loads(call(env, body).body)['data']['acceptance']['status'] == 'duplicate'
    changed = copy.deepcopy(body)
    changed['terminal']['usage']['total_prompt_tokens'] += 1
    changed['terminal']['usage']['uncached_input_tokens'] += 1
    with pytest.raises(HTTPException) as exc:
        call(env, changed)
    assert exc.value.status_code == 409
    assert _drain_async()['outcomes'] == {'settled_now': 1}
    assert env[0].get_gateway_authorization(auth.id).finalized_cost_microdollars == body['terminal']['charge_micro']
    before = save(env[1])
    assert call(env, body).status_code == 200
    assert save(env[1]) == before


@pytest.mark.parametrize('native', [False, True])
def test_decide_four_path_billing_state(env, monkeypatch, native):
    env[3].async_settle_decide_route = True
    run_four_paths(env, monkeypatch, decide_case(native))


@pytest.mark.parametrize('native', [False, True])
@pytest.mark.parametrize('finalizer', ['sync', 'drain'])
def test_decide_records_and_snapshot_sync_after_setting_off(env, monkeypatch, native, finalizer):
    from trusted_router.storage_gcp_analytics_outbox import SpannerAnalyticsOutbox

    env[0].generation_store._analytics_outbox = SpannerAnalyticsOutbox(env[1], env[0]._param_types)
    env[3].async_settle_decide_route = True
    body, auth, _ = prepare(env, decide_case(native), authorize={
        'tags': {'user': 'fixture-user', 'session_id': 'fixture-session'},
    })
    retention_clock(monkeypatch)
    catalog(monkeypatch, body)
    initial = save(env[1])
    client = _client(env[3])
    client.app.state.async_settle = env[2]
    legacy = legacy_body(body)
    legacy['finish_reason'] = 'stop'
    assert client.post('/v1/internal/gateway/settle', json=legacy).status_code == 200
    expected = save(env[1])
    assert expected['generation_records'] and expected['operational_analytics_outbox']
    assert expected['analytics_outbox']
    restore(env[1], initial)
    if finalizer == 'drain':
        assert call(env, body).status_code == 202
    env[3].async_settle_decide_route = False
    if finalizer == 'sync':
        assert json.loads(call(env, body).body)['data']['reason'] == 'unsupported_cohort'
        reply = client.post('/v1/internal/gateway/settle', json=body,
                            headers={'X-TR-Settlement-Mode': 'sync'})
        assert reply.status_code == 200, reply.text
    else:
        assert _drain_async()['outcomes'] == {'settled_now': 1}
    actual = save(env[1])
    if finalizer == 'drain':
        # Fleet health publication belongs to the worker pass, not this settlement.
        for identity in ('fleet-v1', 'health-publish-v1'):
            assert actual['rows'].pop(('settle_drain_control', identity)) is not None
    for name in ('typed', 'rows', 'reservations', 'gateway_authorizations', 'generation_records',
                 'operational_analytics_outbox', 'analytics_outbox'):
        # The fake DML and mutation paths represent TIMESTAMP as str and datetime.
        def stored(value):
            if isinstance(value, datetime):
                return value.isoformat().replace('+00:00', 'Z')
            if isinstance(value, dict):
                return {k: stored(v) for k, v in value.items()}
            if isinstance(value, list):
                return [stored(v) for v in value]
            return value
        assert stored(actual[name]) == stored(expected[name]), name
    gen = env[0].get_generation(body['terminal']['generation_id'])
    assert (gen.route_type, gen.streamed, gen.finish_reason) == ('decide', False, 'stop')
    assert gen.tokens_completion == (7 if native else 0)
    assert env[0].get_gateway_authorization(auth.id).settled


def wire_fixture():
    from trusted_router.catalog import MODELS
    from trusted_router.routes.internal import gateway

    # Wire prices are fixed test inputs; catalog parity is checked separately.
    ep = replace(HOSTED, prompt_price_microdollars_per_million_tokens=44310,
                 completion_price_microdollars_per_million_tokens=0,
                 request_price_microdollars=0, price_tiers=())
    body = hosted_body()
    model = MODELS[ep.model_id]
    estimate = gateway._endpoint_cost_microdollars(
        ep, body.estimated_input_tokens, 1, effective_at=datetime.fromtimestamp(NOW, UTC))
    auth = replace(authorization(), model_id=ep.model_id, requested_model_id=ep.model_id,
                   provider=ep.provider, endpoint_id=ep.id,
                   candidate_endpoint_ids=[ep.id], candidate_model_ids=[ep.model_id],
                   idempotency_key=body.idempotency_key, created_at=datetime.fromtimestamp(NOW, UTC).isoformat(),
                   region='us-central1', estimated_microdollars=estimate)
    response = gateway._gateway_authorize_response(
        authorization=auth, workspace_id=auth.workspace_id, key_hash=auth.key_hash,
        model=model, endpoint=ep, requested_model_id=ep.model_id,
        model_usage_type=UsageType.CREDITS, limit_usage_type=UsageType.CREDITS, estimate=estimate,
        credit_reservation_id=auth.credit_reservation_id, byok_config=None, region='us-central1',
        settings=settings(async_settle_decide_route=True), broadcast_destinations=[],
        endpoint_candidates=[(model, ep)], idempotent_replay=False, custom_model=None,
        async_request=request(runtime()), async_body=body, async_effective_at=datetime.fromtimestamp(NOW, UTC),
    )
    reply = response['data']
    assert reply['async_eligible'] is True
    snapshot = billing.parse_snapshot(json.dumps(reply['billing_snapshot']))
    raw = billing.RawUsage(input_tokens=1234, output_tokens=0)
    observed = billing.Eligibility(route_type='decide', streamed=False)
    result = billing.evaluate(snapshot, ep.id, raw, observed)
    claims = verify_lookup_ticket(reply['settlement_ticket'], [signer().trusted], NOW)
    shared = {k: v for k, v in claims.model_dump().items() if k in billing.TerminalEnvelope.model_fields}
    terminal = billing.TerminalEnvelope(**shared, v=1, selected_endpoint=ep.id,
        usage=result.usage, charge_micro=result.charge_micro, terminal_kind='settle')
    body = dict(billing_snapshot=reply['billing_snapshot'], settlement_ticket=reply['settlement_ticket'],
                terminal=terminal.model_dump(), raw_usage=raw.model_dump(), observed=observed.model_dump())
    parsed = async_settle_handler.parse(canonical(body))
    async_settle_handler.verify(parsed, runtime(), 'settle', NOW)
    assert async_settle_handler.price(parsed) == result.charge_micro
    return dict(seed_hex=FIXTURE['seed_hex'], public_key=FIXTURE['public_key'], now=NOW,
                authorize_request=hosted_body().model_dump(exclude_unset=True),
                authorize_response=response,
                claims=claims.model_dump(), terminal=terminal.model_dump(), settle_request=body)


def test_decide_literal_wire_fixture(env, monkeypatch):
    monkeypatch.setattr(async_settle.time, 'time', lambda: NOW)
    assert canonical(wire_fixture()) + b'\n' == FIXTURE_PATH.read_bytes()


def test_shadow_contract_remains_two_routes():
    from tests import test_async_settle_shadow as shadow
    from tests.test_async_settle_shadow_cohort_runtime import mapped
    from trusted_router.async_settle_shadow_binding import ShadowClaims
    from trusted_router.async_settle_shadow_wire import Rejection, parse_header

    # Existing signed bytes and their canonical terminal still round-trip.
    parsed = parse_header(shadow.wire())
    assert billing.canonical_bytes(parsed.terminal) == canonical(shadow.FIXTURE['terminal'])
    assert shadow.signer().sign(shadow.verify_binding(
        shadow.FIXTURE['billing_shadow_binding'], [shadow.signer().trusted], NOW).model_dump(), NOW
    ) == shadow.FIXTURE['billing_shadow_binding']
    claims = shadow.verify_binding(shadow.FIXTURE['billing_shadow_binding'], [shadow.signer().trusted], NOW)
    with pytest.raises(ValueError):
        ShadowClaims.model_validate({**claims.model_dump(), 'route_type': 'decide'})
    for amount, reason in [(1, 'json_shape'), (True, 'integer')]:
        value = copy.deepcopy(shadow.FIXTURE)
        value['terminal'].update(route_type='decide', charge_micro=amount)
        with pytest.raises(Rejection, match=reason):
            parse_header(shadow.wire(value))
    for stream in (False, True):
        original = mapped(route_type='embeddings', stream=stream)
        actual = mapped(route_type='decide', stream=stream)
        assert original.facts is not None and actual.facts is not None
        expected = {**original.facts.object(), 'route': 'decide'}
        assert actual == replace(original, facts=type(original.facts).from_object(expected))
        assert actual.reason == 'unsupported_route'


def test_snapshot_projection_obeys_decide_setting():
    snapshot = billing.BillingSnapshot.model_validate(decide_case()['snapshot'])
    for enabled in (False, True):
        reply = async_settle.snapshot_projection(
            authorization=authorization(), snapshot=snapshot, requested=billing.Eligibility(route_type='decide'),
            runtime=runtime(), settings=settings(async_settle_decide_route=enabled), now=NOW,
        )
        assert reply['async_eligible'] is enabled
        if not enabled:
            assert reply == {'async_eligible': False}
