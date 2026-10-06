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
from trusted_router.detached_jws import TrustedKey, canonical
from trusted_router.services.async_settle import (
    Admission,
    AdmissionCache,
    DrainHealth,
    Runtime,
    load_runtime,
    projection,
    snapshot_projection,
)
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
    assert canonical(response) == canonical(FIXTURE['response'])  # signature also deterministic
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
    assert canonical({'data': {'authorization_id': 'auth-v1', 'generation_id': CLAIMS['generation_id'], **output}}) == canonical(builder['response'])
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
        parts[1] = base64.urlsafe_b64encode(canonical({**CLAIMS, 'workspace_id': 'wrong'})).rstrip(b'=').decode()
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


def test_shadow_grant_as_ticket_rejected():
    from trusted_router import detached_jws as jws
    from trusted_router import speculation_protocol as protocol
    from trusted_router.services.speculation_shadow import ShadowSigner

    bundle = json.loads((Path(__file__).parent / 'fixtures/speculation_v1/grant-permit-tokens.json').read_text())
    private = signer().private
    shadow_key = protocol.TrustedKey(
        'shadow-test', 'shadow-grant', FIXTURE['public_key'],
        **{field: bundle['grant_claims'][field] for field in ('iss', 'aud', 'environment', 'plane')},
    )
    token = ShadowSigner(private, shadow_key).sign(bundle['grant_claims'], bundle['context'], bundle['now'])
    trusted = jws.TrustedKey(shadow_key.kid, shadow_key.purpose, shadow_key.public_key_b64url,
                             shadow_key.iss, shadow_key.aud)
    with pytest.raises(ValueError, match='^type$'):
        verify_ticket(token, [trusted], CLAIMS, CLAIMS['iat'])
    # Exercise purpose independently of typ and schema. A valid shadow grant
    # cannot acquire ticket authority even if its wire type is allowed here.
    with pytest.raises(ValueError, match='^purpose$'):
        jws.verify(token, [trusted], protocol.SHADOW_TYP, PURPOSE)
    ticket = signer().sign(CLAIMS, CLAIMS['iat'])
    with pytest.raises(protocol.ProtocolError, match='^type$'):
        protocol.verify_grant(ticket, [shadow_key], bundle['context'], bundle['now'], shadow=True)


@pytest.mark.parametrize('seed', range(256))
def test_jws_canonical_and_base64_differential(seed):
    import random

    from trusted_router import detached_jws as jws
    from trusted_router import speculation_protocol as protocol

    rng = random.Random(seed)  # noqa: S311 - deterministic serialization corpus
    alphabet = 'abcXYZ09"\\\n\t é漢😀'
    words = [''.join(rng.choices(alphabet, k=rng.randrange(1, 40))) for _ in range(8)]
    items = [(str(i) + word, [words[::-1], rng.randrange(-(1 << 100), 1 << 100),
                            bool(i % 2), None, {'z': i, 'a': words[i]}])
             for i, word in enumerate(words)]
    rng.shuffle(items)
    claims = dict(items)
    reordered = {key: [value[0], value[1], value[2], value[3], dict(reversed(list(value[4].items())))]
                 for key, value in reversed(items)}
    raw = jws.canonical(claims)
    assert raw == protocol._canonical(claims) == jws.canonical(reordered)
    assert raw.isascii()
    assert jws.canonical(True) != jws.canonical(1)
    assert jws.b64encode(raw) == protocol._b64encode(raw)
    assert jws.b64decode(jws.b64encode(raw)) == raw
    binary = bytes(rng.randrange(256) for _ in range(seed + 1))
    assert jws.b64encode(binary) == protocol._b64encode(binary)
    assert jws.b64decode(jws.b64encode(binary)) == binary


def raw_jws(header=None, payload=None):
    from trusted_router import detached_jws as jws
    from trusted_router.async_settle_ticket import TYP

    header = jws.canonical({'alg': 'EdDSA', 'kid': signer().trusted.kid, 'typ': TYP}) if header is None else header
    payload = jws.canonical(CLAIMS) if payload is None else payload
    material = jws.b64encode(header) + '.' + jws.b64encode(payload)
    return material + '.' + jws.b64encode(signer().private.sign(material.encode('ascii')))


@pytest.mark.parametrize(('header', 'reason'), [
    ({'extra': 'field'}, 'fields'), ({'alg': 'none'}, 'algorithm'),
    ({'alg': 'HS256'}, 'algorithm'), ({'kid': 'wrong'}, 'key'),
    ({'typ': 'wrong'}, 'type'), ({'kid': ''}, 'string'), ({'kid': True}, 'string'),
    ({'kid': '<bad>'}, 'string'),
    (b'{"typ":"tr-async-settle-v1","kid":"async-v1-fixture","alg":"EdDSA"}', 'canonical_header'),
    (b'[]', 'fields'),
])
def test_jws_rejects_headers(header, reason):
    from trusted_router import detached_jws as jws
    from trusted_router.async_settle_ticket import TYP

    if isinstance(header, dict):
        header = jws.canonical({'alg': 'EdDSA', 'kid': signer().trusted.kid, 'typ': TYP, **header})
    with pytest.raises(ValueError, match='^' + reason + '$'):
        verify_ticket(raw_jws(header=header), [signer().trusted], CLAIMS, CLAIMS['iat'])


@pytest.mark.parametrize('encoded', ['', 'Zg=', 'Zg==', 'Zh', 'Zm9', 'A', 'a+b', 'a/b', 'Zg\n', 'é'])
def test_jws_rejects_noncanonical_base64(encoded):
    from trusted_router import detached_jws as jws
    from trusted_router import speculation_protocol as protocol

    for decode in (jws.b64decode, protocol._b64decode):
        with pytest.raises(ValueError, match='^base64$'):
            decode(encoded)


@pytest.mark.parametrize(('payload', 'reason'), [
    (b'{"a":1,"a":2}', 'duplicate_key'), (b'{"a":', 'json'),
    (b'{"a":NaN}', 'integer'), (b'{"a":Infinity}', 'integer'),
    (b'{"a":1.5}', 'integer'), (b'{"a":-1}', 'integer'),
    (b'{"a":9223372036854775808}', 'integer'), (b'{"a":100000000000000000000}', 'integer'),
    (b'{"a":"\\u0061"}', 'json'), (b'{"a":"\xff"}', 'json'),
    (b'[' * 17 + b']' * 17, 'json'), (b'[]', 'fields'),
])
def test_jws_malformed_payload_parity(payload, reason):
    from trusted_router import detached_jws as jws
    from trusted_router import speculation_protocol as protocol
    from trusted_router.async_settle_ticket import TYP

    trusted = signer().trusted
    old_key = protocol.TrustedKey(trusted.kid, trusted.purpose, trusted.public_key_b64url)
    token = raw_jws(payload=payload)
    for verify, key in ((jws.verify, trusted), (protocol._verify, old_key)):
        with pytest.raises(ValueError, match='^' + reason + '$'):
            verify(token, [key], TYP, PURPOSE)


@pytest.mark.parametrize('change', ['not-string', 'too-long', 'parts', 'duplicate-kid', 'truncated',
                                    'bad-public-key', 'reordered-payload', 'padded-header',
                                    'padded-payload', 'padded-signature'])
def test_jws_remaining_negative_cases(change):
    from trusted_router import detached_jws as jws

    token = raw_jws()
    keys = [signer().trusted]
    if change == 'not-string':
        token = None
    elif change == 'too-long':
        token = 'a' * 65537 + '.b.c'
    elif change == 'parts':
        token += '.extra'
    elif change == 'duplicate-kid':
        keys *= 2
    elif change == 'truncated':
        h, p, s = token.split('.')
        token = h + '.' + p + '.' + jws.b64encode(jws.b64decode(s)[:-1])
    elif change == 'bad-public-key':
        keys = [replace(keys[0], public_key_b64url=jws.b64encode(b'short'))]
    elif change == 'reordered-payload':
        token = raw_jws(payload=json.dumps(CLAIMS).encode('ascii'))
    else:
        parts = token.split('.')
        parts[{'padded-header': 0, 'padded-payload': 1, 'padded-signature': 2}[change]] += '='
        token = '.'.join(parts)
    with pytest.raises(ValueError):
        verify_ticket(token, keys, CLAIMS, CLAIMS['iat'])


@pytest.mark.parametrize('value', [float('nan'), float('inf'), float('-inf')])
def test_canonical_rejects_nonfinite_numbers(value):
    from trusted_router.detached_jws import canonical

    with pytest.raises(ValueError):
        canonical({'value': value})
