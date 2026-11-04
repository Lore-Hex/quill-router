"""Negotiate on the enclave's stored region, before it binds async settlement."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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
from trusted_router.enclave_regions import (
    ENCLAVE_REGIONS,
    ENCLAVE_REGIONS_WITHOUT_LOCAL_CONTROL_PLANE,
)
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


ROLLOUT = Path(__file__).resolve().parents[1] / 'scripts' / 'deploy' / 'rollout.sh'


def deployed_pin(name: str) -> str:
    """The value rollout.sh deploys for name, from its one "NAME=value" literal."""
    values = [line.strip().strip('"').split('=', 1)[1] for line in ROLLOUT.read_text().splitlines()
              if line.strip().startswith(f'"{name}=')]
    assert len(values) == 1, values
    return values[0]


def deployed_negotiation_regions() -> set[str]:
    value = deployed_pin('TR_ASYNC_SETTLE_NEGOTIATION_REGIONS')
    return {part.strip() for part in value.split(',') if part.strip()}


def test_the_deployed_negotiation_regions_name_enclave_regions():
    # The exact value is pinned by test_deploy_script_execution.py
    # (test_rollout_negotiation_regions_are_literal_and_configured). This test
    # checks two other things. A region no enclave runs in would never
    # negotiate. An empty value would negotiate everywhere, Europe included.
    named = deployed_negotiation_regions()
    assert named
    assert named <= set(ENCLAVE_REGIONS)


@pytest.mark.parametrize('region', [*ENCLAVE_REGIONS, 'southamerica-east1', '', None])
def test_the_deployed_negotiation_regions_decide_the_projection(region):
    auth = authorization()
    auth.region = region
    snapshot = BillingSnapshot.model_validate(FIXTURE['response']['data']['billing_snapshot'])
    rt = runtime()

    def answer(config):
        return snapshot_projection(authorization=auth, snapshot=snapshot, requested=Eligibility(),
                                   runtime=rt, settings=config, now=2000)

    actual = answer(settings(async_settle_pilot_workspaces='ws-v1',
                             async_settle_negotiation_regions=deployed_pin('TR_ASYNC_SETTLE_NEGOTIATION_REGIONS')))
    if region in deployed_negotiation_regions():
        assert actual['async_eligible'] is True
        assert actual['settlement_ticket']
        assert actual['settlement_status_url']
    else:
        # A region outside negotiation gets the non-pilot answer byte for byte.
        expected = answer(settings(async_settle_pilot_workspaces='non-pilot'))
        assert json.dumps(actual) == json.dumps(expected)
        assert actual['async_eligible'] is False


def terminal_route_enabled() -> bool:
    """The terminal route's switch (infra/control_terminal_backend.tf), read as the renderer reads it."""
    path = ROLLOUT.parent / 'service_surface_url_map.py'
    spec = importlib.util.spec_from_file_location('service_surface_url_map', path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.terminal_route_enabled()


def test_every_negotiated_region_settles_where_it_is_accepted():
    # With the terminal route on, every enclave's settle lands in us-central1.
    # With it off, a region settles in its own router. The exception is a
    # region with no control plane of its own, which settles through
    # us-central1. A negotiated region whose router would refuse its settles
    # is a pin to revisit (docs/runbooks/control-terminals-us-central1.md).
    route_on = terminal_route_enabled()
    accept = deployed_pin('TR_ASYNC_SETTLE_ACCEPT_REGIONS')
    for region in sorted(deployed_negotiation_regions()):
        lands = 'us-central1' if route_on or region in ENCLAVE_REGIONS_WITHOUT_LOCAL_CONTROL_PLANE else region
        config = settings(async_settle_accept_regions=accept, serving_region=lands, primary_region='us-central1')
        assert config.async_settle_accept_region_allowed(), (region, lands, route_on)
