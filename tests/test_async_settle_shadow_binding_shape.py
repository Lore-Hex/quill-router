"""Router-produced bindings with production identity formats, shared with Go."""
from __future__ import annotations

import base64
import hashlib
import json
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tests.test_async_settle_shadow_authorize_context import CASES, authorize, probe_context
from tests.test_async_settle_shadow_cohort_differential import registered_apps  # noqa: F401
from trusted_router import security
from trusted_router.async_settle_shadow_binding import ShadowSigner, verify_binding
from trusted_router.detached_jws import canonical
from trusted_router.services.async_settle import load_runtime
from trusted_router.storage import STORE, typed_billing_store

FIXTURE_PATH = Path(__file__).parent / 'fixtures/async_settlement/shadow_binding_production.json'
FIXTURE_SHA256 = 'd9dd89600421ac68da087f02dac99288241307f5804d24210006ca99cf163fb0'
# Public deterministic test material only. Producers retain their normal encoding.
SEED = bytes([1]) * 32
WORKSPACE = str(uuid.UUID(bytes=bytes(range(16)), version=4))
REQUEST_UUID = uuid.UUID(bytes=bytes(range(16, 32)), version=4)


def produce_binding(monkeypatch, app_factory, tmp_path, name, *, origin='typed'):
    monkeypatch.setattr(security.secrets, 'token_urlsafe',
        lambda size: base64.urlsafe_b64encode(bytes(range(size))).rstrip(b'=').decode())
    monkeypatch.setattr(security.secrets, 'token_hex', lambda size: bytes(range(size)).hex())
    with probe_context(monkeypatch, app_factory, name, workspace_id=WORKSPACE) as env:
        # gateway._new_gateway_authorization_id and the typed reservation producer
        # still run; pin only their uuid4 entropy, with real UUIDv4 variant bits.
        monkeypatch.setattr(uuid, 'uuid4', lambda: REQUEST_UUID)
        backend = typed_billing_store(STORE)
        assert backend is not None
        monkeypatch.setattr(backend, 'request_record_write_mode', origin)
        private = Ed25519PrivateKey.from_private_bytes(SEED)
        pem = tmp_path / 'binding-test-key.pem'
        pem.write_bytes(private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        cfg = env.cfg
        cfg.async_settle_ticket_private_key_file = str(pem)
        # Exact deployment settings, not the short historical fixture issuer/kid.
        cfg.async_settle_ticket_kid = 'tr-async-settle-2026-10a'
        cfg.async_settle_ticket_issuer = 'https://api.trustedrouter.com'
        cfg.async_settle_ticket_audience = 'router-settlement'
        cfg.async_settle_authority_epoch = 1
        cfg.primary_region = 'us-central1'
        cfg.serving_region = 'us-central1'
        runtime = load_runtime(cfg, None)
        assert runtime.signer is not None
        env.shell.async_runtime = runtime
        env.shell.signer = ShadowSigner(runtime.signer)
        env.client.app.state.async_settle = runtime
        response = authorize(env)
        data = response.json()['data']
        event = env.events()[0]
        if origin == 'legacy':
            assert event['eligibility'] != 'eligible'
            assert 'billing_shadow_binding' not in data
            return None
        assert event['eligibility'] == 'eligible', event['diagnostic']
        token = data['billing_shadow_binding']
        parsed = verify_binding(token, [env.shell.signer.trusted], env.mapped[0].observed_us // 1000000)
        assert parsed.authorization_id == 'gwa-' + REQUEST_UUID.hex
        assert parsed.workspace_id == WORKSPACE
        assert parsed.key_id == security.new_key_id()
        assert parsed.reservation_id == str(REQUEST_UUID)
        assert parsed.journal_region == cfg.primary_region and parsed.epoch == cfg.async_settle_authority_epoch
        assert parsed.settle_origin == 'typed'
        fields = ('authorization_id', 'generation_id', 'workspace_id', 'api_key_hash',
            'credit_reservation_id', 'invocation_nonce', 'model', 'provider', 'endpoint_id',
            'billing_snapshot', 'billing_snapshot_hash', 'billing_shadow_binding', 'billing_shadow_policy')
        return dict(name=name, request=env.payload, claims=parsed.model_dump(),
            authorization={k: data[k] for k in fields}, binding=token)


def test_binding_fixture_pin():
    assert hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest() == FIXTURE_SHA256


@pytest.mark.parametrize('name', CASES)
def test_production_binding_bytes(monkeypatch, registered_apps, tmp_path, name):  # noqa: F811
    expected = next(c for c in json.loads(FIXTURE_PATH.read_bytes())['cases'] if c['name'] == name)
    actual = produce_binding(monkeypatch, registered_apps, tmp_path, name)
    assert canonical(actual) == canonical(expected)


@pytest.mark.parametrize('name', CASES)
def test_legacy_origin_cannot_issue_binding(monkeypatch, registered_apps, tmp_path, name):  # noqa: F811
    assert produce_binding(monkeypatch, registered_apps, tmp_path, name, origin='legacy') is None
