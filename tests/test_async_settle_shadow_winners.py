# ruff: noqa: F811 - imported pytest fixture
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from tests.test_async_settle_handler import env, prepare, row_for  # noqa: F401
from tests.test_async_settle_oracle import endpoint_from_candidate
from tests.test_async_settle_shadow import FIXTURE, NOW, signer, wire
from tests.test_async_settle_shadow_accounting import Database
from tests.test_settle_outbox_drain import _client, _typed_credit, _typed_key
from trusted_router.async_settle_shadow_binding import verify_binding
from trusted_router.async_settle_shadow_compare import Booking
from trusted_router.catalog_data import Model
from trusted_router.detached_jws import canonical
from trusted_router.routes.internal import gateway
from trusted_router.services import async_settle_shadow as module
from trusted_router.services.async_settle_shadow import Runtime
from trusted_router.storage_gcp_async_settle_shadow import EvidenceStore
from trusted_router.storage_models import generation_id_for_authorization


@pytest.mark.parametrize('first', ['settle', 'refund'])
@pytest.mark.parametrize('reverse_delivery', [False, True])
@pytest.mark.parametrize('zero', [False, True])
def test_real_winner_then_delayed_shadow_and_lost_ack(env, monkeypatch, first, reverse_delivery, zero):
    body, auth, key = prepare(env)
    _, db, rt, cfg = env
    repair = json.loads(row_for(env, body).settle_body)
    cfg.async_settle_enabled = cfg.async_settle_protection = False
    cfg._async_settle_shadow_workspace_ids = frozenset({'ws-v1'})
    cfg.release = 'a'*40
    payload = json.loads(db.gateway_authorizations[auth.id]['payload'])
    payload['invocation_nonce'] = auth.invocation_nonce
    db.gateway_authorizations[auth.id]['payload'] = json.dumps(payload)
    endpoints = {c['endpoint_id']: endpoint_from_candidate(c) for c in body['billing_snapshot']['candidates']}
    for endpoint in endpoints.values():
        monkeypatch.setitem(gateway.MODELS, endpoint.model_id, Model(id=endpoint.model_id, name='shadow', provider=endpoint.provider, context_length=1000000, prepaid_available=True))
    monkeypatch.setattr(gateway, 'endpoint_for_id', endpoints.get)
    if zero:
        repair.update(actual_input_tokens=0, actual_output_tokens=0, cache_read_input_tokens=0, cache_creation_input_tokens=0, reasoning_tokens=0)
    claims = verify_binding(FIXTURE['billing_shadow_binding'], [signer().trusted], NOW).model_dump()
    claims.update(authorization_id=auth.id, generation_id=generation_id_for_authorization(auth.id),
        key_id=auth.key_hash, reservation_id=auth.credit_reservation_id, invocation_nonce=auth.invocation_nonce,
        snapshot_hash=body['terminal']['snapshot_hash'])
    proof = signer().sign(claims, NOW)
    store = EvidenceStore(Database())
    def booking(*args):
        record = db.gateway_authorizations[auth.id]
        return Booking(record['finalized_cost_microdollars'], record['finalization_outcome'], record['settled'])
    monkeypatch.setattr(store, 'booking', booking)
    shadow = Runtime(cfg, rt, store)
    shadow.signer = signer()
    client = _client(cfg)
    client.app.state.async_settle_shadow = shadow
    pending, observations = [], []
    original_submit, original_compare = shadow.submit, module.compare
    monkeypatch.setattr(shadow, 'submit', lambda *args: pending.append(args))
    def compare(*args):
        result = original_compare(*args)
        observations.append((args[1].attempted_kind, result.classification, result.booked_micro, result.booked_minus_frozen))
        return result
    monkeypatch.setattr(module, 'compare', compare)
    other = 'refund' if first == 'settle' else 'settle'
    kinds = [first, other, first]  # First claim, losing claim, lost-ack replay.
    expected = 2 if first == 'settle' and not zero else 0
    for kind in kinds:
        envelope = copy.deepcopy(FIXTURE)
        envelope.update(billing_snapshot=body['billing_snapshot'], billing_shadow_binding=proof, terminal=copy.deepcopy(body['terminal']))
        envelope['terminal'].update(terminal_kind=kind, charge_micro=2 if kind=='settle' and not zero else 0)
        if zero:
            envelope['raw_usage'] = dict.fromkeys(envelope['raw_usage'], 0)
            envelope['terminal']['usage'] = dict.fromkeys(envelope['terminal']['usage'], 0)
        envelope['payload_hash'] = hashlib.sha256(canonical(envelope['terminal'])).hexdigest()
        response = client.post('/v1/internal/gateway/'+kind, json=repair, headers={'X-TR-Settlement-Shadow':wire(envelope)[0]})
        assert response.status_code == 200 and response.json()['data']['cost_microdollars'] == expected
    assert (_typed_credit(db,'ws-v1')['total_usage'], _typed_key(db,key.hash)['usage'], db.reservations[auth.credit_reservation_id]['actual_micro']) == (expected, expected, expected)
    assert db.gateway_authorizations[auth.id]['finalization_outcome'] == ('settled' if first=='settle' else 'refunded')
    before = copy.deepcopy(db.typed)
    class Background:
        def add_task(self, function, capture, headers, result, elapsed, dims, size):
            shadow.process(capture, headers, result, elapsed, dims)
            shadow.pending -= 1
            shadow.queued_bytes -= size
    order = [1, 0, 2] if reverse_delivery else [0, 1, 2]
    ordered = [pending[index] for index in order]
    for capture, request, result, _ in ordered:
        original_submit(capture, request, result, Background())
    delivered = [kinds[index] for index in order]
    assert observations == [(kind, 'exact' if kind==first else 'requires_review', expected, 0 if kind==first else None) for kind in delivered]
    assert db.typed == before
    shadow.executor.shutdown()
