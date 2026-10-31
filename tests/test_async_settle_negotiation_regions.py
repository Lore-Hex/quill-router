"""Negotiate on the enclave's stored region, before it binds async settlement."""
from __future__ import annotations

import json

import pytest

from tests.test_async_settle_ticket import FIXTURE, authorization, runtime, settings
from tests.test_gateway_authorize_spanner_operations import (
    _body,
    _seed_typed_gateway_store,
    fixed_operation_catalog,  # noqa: F401 - shared routing fixture
)
from tests.test_settle_outbox_drain import _client
from trusted_router.billing_snapshot import BillingSnapshot, Eligibility
from trusted_router.config import Settings
from trusted_router.services.async_settle import snapshot_projection


@pytest.mark.parametrize('region,allowed', [
    ('us-central1', True), ('us-west1', True), ('europe-west4', False),
    ('us-east4', False), ('southamerica-east1', False), ('', False), (None, False),
])
@pytest.mark.parametrize('raw', ['', 'us-central1,us-west1'])
def test_negotiation_regions_from_environment(monkeypatch, raw, region, allowed):
    monkeypatch.setenv('TR_ASYNC_SETTLE_NEGOTIATION_REGIONS', raw)
    config = Settings(environment='test', _env_file=None)
    assert config.async_settle_negotiation_region_allowed(region) is (not raw or allowed)


@pytest.mark.parametrize('raw', [
    'US-central1', 'us_central1', 'us-central1/a', 'us central1', 'a' * 33, 'é',
])
def test_negotiation_regions_reject_invalid_tokens_like_shadow(raw):
    with pytest.raises(ValueError, match='invalid async settle shadow region set'):
        settings(async_settle_shadow_regions=raw)
    with pytest.raises(ValueError, match='invalid async settle negotiation region set'):
        settings(async_settle_negotiation_regions=raw)


@pytest.mark.parametrize('raw,region,allowed', [
    (' , , ', None, True),
    (' us-central1,us-west1,us-central1, ', 'us-central1', True),
    (' us-central1,us-west1,us-central1, ', 'us-west1', True),
    (' us-central1,us-west1,us-central1, ', 'us-east4', False),
    ('a' * 32, 'a' * 32, True),
])
def test_negotiation_regions_parse_like_shadow(raw, region, allowed):
    config = settings(async_settle_negotiation_regions=raw, async_settle_shadow_regions=raw)
    assert config.async_settle_negotiation_region_allowed(region) is allowed


@pytest.mark.parametrize('region', [
    'europe-west4', 'us-east4', 'us-central1', 'us-west1', 'southamerica-east1', '', None,
])
@pytest.mark.parametrize('raw', ['', 'us-central1,us-west1'])
def test_negotiation_regions_projection_matches_existing_shapes(raw, region):
    auth = authorization()
    auth.region = region
    snapshot = BillingSnapshot.model_validate(FIXTURE['response']['data']['billing_snapshot'])
    rt = runtime()

    def answer(config):
        return snapshot_projection(authorization=auth, snapshot=snapshot, requested=Eligibility(),
                                   runtime=rt, settings=config, now=2000)

    actual = answer(settings(async_settle_pilot_workspaces='ws-v1',
                             async_settle_negotiation_regions=raw))
    if raw and region not in {'us-central1', 'us-west1'}:
        # Same authorization and snapshot; only the pilot membership differs.
        expected = answer(settings(async_settle_pilot_workspaces='non-pilot'))
        assert actual == expected
        assert json.dumps(actual) == json.dumps(expected)
        assert set(actual) == {'billing_snapshot', 'billing_snapshot_hash', 'async_eligible'}
        assert actual['async_eligible'] is False
        assert not rt.admission.entries
    else:
        expected = answer(settings(async_settle_pilot_workspaces='ws-v1'))
        assert actual == expected
        assert actual['async_eligible'] is True
        assert actual['settlement_ticket']
        assert actual['settlement_status_url']


@pytest.mark.usefixtures('fixed_operation_catalog')
def test_negotiation_regions_real_authorize_route():
    store, _, key = _seed_typed_gateway_store()
    config = settings(async_settle_pilot_workspaces='ws-rpc',
                      async_settle_negotiation_regions='us-central1,us-west1',
                      regions='us-central1,us-east4,europe-west4,us-west1',
                      primary_region='us-central1', serving_region='us-central1')
    client = _client(config)
    client.app.state.async_settle = runtime()
    try:
        for region, eligible in [('europe-west4', False), ('us-central1', True)]:
            body = _body(key.hash, idempotency_key='negotiation-' + region)
            body.region = region
            body.route_type = 'chat.completions'
            body.invocation_nonce = 'nonce-' + region
            response = client.post('/v1/internal/gateway/authorize',
                                   json=body.model_dump(mode='json', exclude_unset=True),
                                   headers={'X-TR-Settlement-Mode': 'async-v1'})
            assert response.status_code == 200, response.text
            data = response.json()['data']
            assert store.get_gateway_authorization(data['authorization_id']).region == region
            assert data['async_eligible'] is eligible
            assert ('settlement_ticket' in data) is eligible
            assert ('settlement_status_url' in data) is eligible
            assert data['billing_snapshot'] and data['billing_snapshot_hash']
    finally:
        client.close()
