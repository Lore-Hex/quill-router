from dataclasses import replace
from decimal import Decimal

import httpx

from trusted_router import token_exchange as exchange
from trusted_router.catalog import endpoints_for_model


def test_prices_are_exact_named_credit_route(monkeypatch):
    model = exchange.MODELS[0][0]
    source = next(e for e in endpoints_for_model(model)
                  if e.provider == 'tinfoil' and e.usage_type == 'Credits')
    named = replace(source, prompt_price_microdollars_per_million_tokens=73850,
                    completion_price_microdollars_per_million_tokens=253200)
    cheaper = replace(named, provider='openai', prompt_price_microdollars_per_million_tokens=1)
    byok = replace(named, usage_type='BYOK', prompt_price_microdollars_per_million_tokens=0)
    monkeypatch.setattr(exchange, 'endpoints_for_model', lambda _: [cheaper, byok, named])
    rows = exchange.confidential_prices()
    assert len(rows) == 3
    assert all(r['input'] == '0.07385' and r['output'] == '0.2532' for r in rows)
    assert all(r['endpoint_id'] == named.id for r in rows)


def test_catalog_parity():
    for row in exchange.confidential_prices():
        endpoint = next(e for e in endpoints_for_model(row['model']) if e.id == row['endpoint_id'])
        assert Decimal(row['input']) * 1_000_000 == endpoint.prompt_price_microdollars_per_million_tokens
        assert Decimal(row['output']) * 1_000_000 == endpoint.completion_price_microdollars_per_million_tokens


def test_missing_ambiguous_or_nonconfidential_route_has_no_fallback(monkeypatch):
    source = next(e for e in endpoints_for_model(exchange.MODELS[0][0])
                  if e.provider == 'tinfoil' and e.usage_type == 'Credits')
    for routes in ([], [source, source]):
        monkeypatch.setattr(exchange, 'endpoints_for_model', lambda _, rows=routes: rows)
        assert exchange.confidential_prices() == []
    monkeypatch.setattr(exchange, 'endpoints_for_model', lambda _: [source])
    monkeypatch.setattr(exchange, 'endpoint_e2ee', lambda _: False)
    assert exchange.confidential_prices() == []


def test_failed_source_never_reuses_prior_data(monkeypatch):
    component = dict(id='canonical_api', last_checked_at='2026-09-28T00:00:00Z')
    monkeypatch.setattr(exchange, '_get', lambda client, url: {'data': {'components': [component]}})
    assert exchange.exchange_evidence('new-york')['components'] == [component]
    monkeypatch.setattr(exchange, '_get', lambda client, url: None)
    failed = exchange.exchange_evidence('new-york')
    assert failed['components'] == []
    assert failed['attestation_check'] is None
    assert failed['release'] is None


def test_get_handles_http_errors_and_invalid_json():
    for response in (httpx.Response(503), httpx.Response(200, content=b'not json')):
        with httpx.Client(transport=httpx.MockTransport(lambda _, result=response: result)) as client:
            assert exchange._get(client, 'https://example.test/status.json') is None


def test_profiles_only_expose_source_components(monkeypatch):
    components = [dict(id=k) for k in ('canonical_api', 'us_east4_regional_api',
                                      'eu_regional_api', 'uaenorth_gateway', 'attestation')]
    monkeypatch.setattr(exchange, '_get', lambda client, url: {'data': {'components': components}})
    for profile, regional in (('new-york', 'us_east4_regional_api'),
                              ('europe', 'eu_regional_api'), ('dubai', 'uaenorth_gateway'),
                              ('shared-gcp', None)):
        result = exchange.exchange_evidence(profile)
        assert {c['id'] for c in result['components']} == ({'canonical_api', regional} if regional else {'canonical_api'})
        assert result['attestation_check']['id'] == 'attestation'
        assert 'model_inference' not in str(result['components'])


def test_malformed_status_and_embedded_release_fail_closed(monkeypatch):
    for invalid in ({'data': []}, {'data': {'components': None}}, {'data': {'components': [None]}}):
        monkeypatch.setattr(exchange, '_get', lambda client, url, data=invalid: (
            data if url.endswith('status.json') else {'release_metadata_status': 'embedded'}))
        payload = exchange.exchange_evidence('new-york')
        assert payload['components'] == []
        assert payload['release'] is None


def test_public_feed_cache_expires_without_stale_fallback(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from trusted_router.config import Settings
    from trusted_router.routes import public

    app = FastAPI()
    public.register_public_routes(app, Settings(environment='local', storage_backend='memory'))
    monkeypatch.setattr(public, '_STATUS_RESPONSE_CACHE', public.OrderedDict())
    monkeypatch.setattr(public, 'exchange_evidence', lambda _: {'components': [{'id': 'canonical_api'}]})
    with TestClient(app) as client:
        url = '/token-exchange/evidence/new-york.json'
        response = client.get(url)
        assert response.status_code == 200
        assert response.headers['cache-control'] == 'no-store'
        assert response.headers['access-control-allow-origin'] == '*'
        assert response.json()['components']
        cached = public._STATUS_RESPONSE_CACHE['exchange:evidence:new-york']
        public._STATUS_RESPONSE_CACHE['exchange:evidence:new-york'] = replace(
            cached, cached_at=cached.cached_at - 61)
        monkeypatch.setattr(public, 'exchange_evidence', lambda _: {'components': []})
        assert client.get(url).json()['components'] == []
        assert client.get('/token-exchange/evidence/not-a-profile.json').status_code == 404


def test_prices_link_to_the_exact_rendered_tinfoil_row():
    from bs4 import BeautifulSoup

    from trusted_router.config import Settings
    from trusted_router.dashboard import public_model_detail_html

    for quote in exchange.confidential_prices():
        page = public_model_detail_html(Settings(environment='test'), quote['model'])
        assert page is not None
        row = BeautifulSoup(page, 'html.parser').select_one('#provider-tinfoil')
        assert row is not None
        text = row.get_text(' ', strip=True)
        assert '$' + quote['input'] + '/1M' in text
        assert '$' + quote['output'] + '/1M' in text
        assert quote['source'].endswith('#provider-tinfoil')
