from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trusted_router.async_settle_ticket import PURPOSE, TicketSigner, verify_ticket
from trusted_router.billing_snapshot import BillingSnapshot, Eligibility, canonical_hash
from trusted_router.catalog_data import ModelEndpoint
from trusted_router.config import Settings
from trusted_router.services.async_settle import (
    Admission,
    AdmissionCache,
    DrainHealth,
    Runtime,
    load_runtime,
    projection,
    snapshot_projection,
)
from trusted_router.speculation_protocol import TrustedKey, _canonical
from trusted_router.storage_models import GatewayAuthorization
from trusted_router.types import UsageType

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/async_settlement/authorize_v1.json').read_text())
CLAIMS = FIXTURE['claims']


def signer():
    key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(FIXTURE['seed_hex']))
    return TicketSigner(key, TrustedKey('async-v1-fixture', PURPOSE, FIXTURE['public_key'],
                                       'router-fixture', 'router-settlement'))


def authorization():
    return GatewayAuthorization(id='auth-v1', workspace_id='ws-v1', key_hash='key-v1',
                                model_id='openai/billing-v1', provider='openai',
                                usage_type=UsageType.CREDITS, estimated_microdollars=2,
                                credit_reservation_id='res-v1', invocation_nonce='nonce-v1')


def endpoint(**changes):
    return replace(ModelEndpoint(id='openai/billing-v1@openai/prepaid', model_id='openai/billing-v1',
                                 provider='openai', usage_type='Credits',
                                 prompt_price_microdollars_per_million_tokens=500000,
                                 completion_price_microdollars_per_million_tokens=500000), **changes)


def runtime():
    cache = AdmissionCache(lambda ws: Admission(0, 2), clock=lambda: 10.0)
    cache.health = DrainHealth(10.0, 0)
    return Runtime(signer(), cache, 'us-central1', 1)


def settings(**kwargs):
    return Settings(environment='test', async_settle_enabled=True, **kwargs)


def test_literal_wire_and_fixture_signature():
    assert hashlib.sha256((Path(__file__).parent / 'fixtures/async_settlement/authorize_v1.json').read_bytes()).hexdigest() == '3f8f7b08aeb89e35bdcecd85848fc5bb95b181e103f8473459af7065a79458e0'
    rt = runtime()
    snapshot = BillingSnapshot.model_validate(FIXTURE['response']['data']['billing_snapshot'])
    additions = snapshot_projection(authorization=authorization(), snapshot=snapshot,
                                   requested=Eligibility(), runtime=rt, settings=settings(),
                                   now=CLAIMS['iat'])
    response = {'data': {'authorization_id': 'auth-v1', 'generation_id': CLAIMS['generation_id'], **additions}}
    assert _canonical(response) == _canonical(FIXTURE['response'])  # signature also deterministic
    ticket = additions['settlement_ticket']
    assert verify_ticket(ticket, [rt.signer.trusted], CLAIMS, FIXTURE['verify_at']).model_dump() == CLAIMS
    assert signer().sign(CLAIMS, CLAIMS['iat']) == ticket


def test_literal_is_not_the_catalog_builder_cache_policy():
    # Explicit design discrepancy, never patch the builder to manufacture parity.
    output = projection(authorization=authorization(), endpoints=[endpoint()], requested=Eligibility(),
                        runtime=runtime(), settings=settings(), now=CLAIMS['iat'])
    rates = output['billing_snapshot']['candidates'][0]['rates']
    assert rates['cached_input_micro_per_million'] == 250000
    assert rates['cache_creation_micro_per_million'] == 625000
    assert output['billing_snapshot_hash'] != CLAIMS['snapshot_hash']
    builder = json.loads((Path(__file__).parent / 'fixtures/async_settlement/authorize_v1_builder.json').read_text())
    assert _canonical({'data': {'authorization_id': 'auth-v1', 'generation_id': CLAIMS['generation_id'], **output}}) == _canonical(builder['response'])
    assert verify_ticket(output['settlement_ticket'], [signer().trusted], builder['claims'], CLAIMS['iat'])
    assert output['billing_snapshot_hash'] == canonical_hash(BillingSnapshot.model_validate(output['billing_snapshot']))


@pytest.mark.parametrize('field', list(CLAIMS))
def test_every_claim_is_bound(field):
    expected = copy.deepcopy(CLAIMS)
    value = expected[field]
    expected[field] = not value if type(value) is bool else value + 1 if type(value) is int else value + '-other'
    with pytest.raises(ValueError):
        verify_ticket(signer().sign(CLAIMS, CLAIMS['iat']), [signer().trusted], expected, CLAIMS['iat'])


@pytest.mark.parametrize('field', list(CLAIMS))
def test_every_claim_required(field):
    claims = dict(CLAIMS)
    del claims[field]
    with pytest.raises(ValueError):
        signer().sign(claims, CLAIMS['iat'])
    with pytest.raises(ValueError):
        verify_ticket(FIXTURE['response']['data']['settlement_ticket'], [signer().trusted], claims, CLAIMS['iat'])


@pytest.mark.parametrize('now', [CLAIMS['iat'] - 1, CLAIMS['exp'], CLAIMS['exp'] + 1])
def test_expiry(now):
    with pytest.raises(ValueError):
        verify_ticket(FIXTURE['response']['data']['settlement_ticket'], [signer().trusted], CLAIMS, now)


@pytest.mark.parametrize('change', ['aud', 'iss', 'purpose', 'key', 'kid', 'altered'])
def test_bad_signatures_and_trust(change):
    token = FIXTURE['response']['data']['settlement_ticket']
    key = signer().trusted
    if change in {'aud', 'iss', 'purpose', 'kid'}:
        key = replace(key, **{change: 'shadow-grant' if change == 'purpose' else 'wrong'})
    elif change == 'key':
        public = Ed25519PrivateKey.from_private_bytes(bytes([2]) * 32).public_key().public_bytes_raw()
        key = replace(key, public_key_b64url=base64.urlsafe_b64encode(public).rstrip(b'=').decode())
    else:
        parts = token.split('.')
        parts[1] = base64.urlsafe_b64encode(_canonical({**CLAIMS, 'workspace_id': 'wrong'})).rstrip(b'=').decode()
        token = '.'.join(parts)
    with pytest.raises(ValueError):
        verify_ticket(token, [key], CLAIMS, CLAIMS['iat'])


@pytest.mark.parametrize('config', ['missing', 'invalid', 'shadow_path', 'shadow_copy', 'shadow_kid', 'valid'])
def test_key_load_outside_request_and_no_secrets(tmp_path, caplog, config):
    private = signer().private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption())
    path = tmp_path / 'ticket.pem'
    path.write_bytes(private if config != 'invalid' else b'sensitive invalid material')
    shadow = tmp_path / 'shadow.pem'
    shadow.write_bytes(private)
    opts = dict(async_settle_ticket_private_key_file=str(path), async_settle_ticket_kid='async-v1-fixture',
                async_settle_ticket_issuer='router-fixture', async_settle_ticket_audience='router-settlement',
                async_settle_authority_epoch=1)
    if config == 'missing':
        opts['async_settle_ticket_private_key_file'] = str(tmp_path / 'absent')
    if config in {'shadow_path', 'shadow_copy'}:
        opts['speculation_shadow_private_key_file'] = str(path if config == 'shadow_path' else shadow)
    if config == 'shadow_kid':
        opts['speculation_shadow_kid'] = 'async-v1-fixture'
    rt = load_runtime(settings(**opts), None)
    assert (rt.signer is not None) == (config == 'valid')
    assert not caplog.text
    path.unlink()
    if config == 'valid':
        assert rt.signer.sign(CLAIMS, CLAIMS['iat'])


def test_no_key_or_authority_never_invalidates_authorize():
    for rt in [None, Runtime(None, None, 'us-central1', 1), Runtime(signer(), None, '', 1),
               Runtime(signer(), None, 'us-central1', 0)]:
        assert projection(authorization=authorization(), endpoints=[endpoint()], requested=Eligibility(),
                          runtime=rt, settings=settings()) == {'async_eligible': False}


def test_rollout_never_inherits_key_file():
    source = Path('scripts/deploy/rollout.sh').read_text()
    lines = [line.strip() for line in source.splitlines() if 'TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE' in line]
    assert lines == ['"TR_ASYNC_SETTLE_TICKET_PRIVATE_KEY_FILE="']
    assert Settings().async_settle_enabled is False


@pytest.mark.parametrize('field', ['authorization_id', 'workspace_id', 'key_id', 'reservation_id'])
def test_ticket_preserves_native_identity_bounds(field):
    claims = {**CLAIMS, field: 'x' * 65}
    with pytest.raises(ValueError):
        signer().sign(claims, CLAIMS['iat'])


def test_status_url_encodes_identity_as_one_path_component():
    auth = authorization()
    auth.id = 'auth/v1'
    output = projection(authorization=auth, endpoints=[endpoint()], requested=Eligibility(),
                        runtime=runtime(), settings=settings(), now=CLAIMS['iat'])
    assert output['settlement_status_url'] == '/v1/settlements/auth%2Fv1.settle'
