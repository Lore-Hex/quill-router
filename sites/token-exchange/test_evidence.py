import copy
import json
import unittest
from pathlib import Path

from build import load_markets, render
from evidence import render_evidence
from refresh_evidence import ROUTES, project


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads(Path(__file__).with_name('evidence.json').read_text())

    def regional_refresh_inputs(self):
        endpoints = [{'data': [dict(
            provider=provider, usage_type='Credits', provider_name='Tinfoil',
            pricing=dict(prompt='0.000001', completion='0.000002'),
            trustedrouter=dict(provider_e2ee=True, provider_confidential_compute=True,
                               provider_zero_data_retention=True,
                               privacy_tier_label='Confidential + E2EE'),
        )]} for _, _, provider in ROUTES]
        components = [dict(
            id=key, name=name, description=name,
            last_checked_at='2026-09-24T00:00:00Z', uptime_24h_percent=uptime,
            sample_count_24h=10, history=[dict(
                bucket_start='2026-09-24T00:00:00Z', status='degraded',
                sample_count=10, uptime_percent=uptime)],
        ) for key, name, uptime in (
            ('canonical_api', 'Canonical API', 98),
            ('model_inference', 'Model Inference', 97),
            ('eu_regional_api', 'EU Regional API', 80),
            ('us_east4_regional_api', 'US East Regional API', 99),
            ('uaenorth_gateway', 'UAE North Gateway (Dubai)', 90),
        )]
        return endpoints, {'data': dict(
            components=components, generated_at='2026-09-24T00:00:00Z')}

    def test_europe_refresh_requires_its_regional_component_and_gcp_release(self):
        endpoints, status = self.regional_refresh_inputs()
        result = project(endpoints, status, '2026-09-24T00:00:00Z',
                         self.data['attestation'], market='europe')
        self.assertEqual(result['status']['name'], 'EU Regional API')
        self.assertEqual(result['status']['uptime'], 80)
        self.assertEqual(result['status']['history'][0]['uptime_percent'], 80)
        self.assertEqual(result['status']['source'], 'https://trustedrouter.com/status.json')
        self.assertEqual(result['uptime_label'], 'GCP uptime')
        self.assertEqual([c['id'] for c in result['service_history']],
                         ['canonical_api', 'model_inference'])
        for release in (None, {'platform': 'azure-confidential-containers-sev-snp'},
                        {'platform': 'aws-nitro-enclaves'}):
            with self.subTest(release=release):
                with self.assertRaisesRegex(ValueError, 'GCP release'):
                    project(endpoints, status, '2026-09-24T00:00:00Z',
                            release, market='europe')
        status['data']['components'] = [c for c in status['data']['components']
                                        if c['id'] != 'eu_regional_api']
        with self.assertRaises(StopIteration):
            project(endpoints, status, '2026-09-24T00:00:00Z',
                    self.data['attestation'], market='europe')

    def test_shared_profile_discards_all_regional_rows_and_requires_shared_history(self):
        endpoints, status = self.regional_refresh_inputs()
        result = project(endpoints, status, '2026-09-24T00:00:00Z',
                         self.data['attestation'], market='shared-gcp')
        self.assertNotIn('status', result)
        self.assertEqual([c['id'] for c in result['service_history']],
                         ['canonical_api', 'model_inference'])
        self.assertEqual(result['uptime_label'], 'Shared GCP uptime')
        for release in (None, {'platform': 'azure-confidential-containers-sev-snp'}):
            with self.subTest(release=release):
                with self.assertRaisesRegex(ValueError, 'GCP release'):
                    project(endpoints, status, '2026-09-24T00:00:00Z',
                            release, market='shared-gcp')
        for missing in ('canonical_api', 'model_inference'):
            incomplete = copy.deepcopy(status)
            incomplete['data']['components'] = [c for c in status['data']['components']
                                                if c['id'] != missing]
            with self.subTest(missing=missing):
                with self.assertRaisesRegex(ValueError, 'canonical API'):
                    project(endpoints, incomplete, '2026-09-24T00:00:00Z',
                            self.data['attestation'], market='shared-gcp')

    def test_europe_snapshot_uses_only_gcp_eu_evidence_in_shared_layout(self):
        markets = load_markets()
        europe = next(m for m in markets if m['slug'] == 'europe')
        page = render(europe, markets, 'test')
        self.assertIn('EU Regional API', page)
        self.assertIn('GCP uptime', page)
        self.assertIn('https://trustedrouter.com/providers', page)
        self.assertIn('https://trustedrouter.com/status', page)
        self.assertIn('https://trustedrouter.com/trust/gcp-release.json', page)
        self.assertIn('Review GCP release', page)
        self.assertIn('Not proven by attestation', page)
        self.assertNotIn('US East Regional API', page)
        self.assertNotIn('UAE North', page)
        self.assertNotIn('Gateway (Ireland)', page)
        self.assertNotIn('Gateway (Paris)', page)
        self.assertNotIn('Published policy measurement', page)
        self.assertEqual(page.count('class="service-history"'), 3)
        self.assertEqual(page.count('As of '), 1)
        self.assertLess(page.index('class="trust-intro"'), page.index('class="catalogue'))
        self.assertIn('exchange_market=europe', page)

    def test_migrated_markets_share_gcp_sources_without_regional_claims(self):
        markets = load_markets()
        shared = {'global', 'chicago', 'san-francisco', 'texas', 'united-states',
                  'hong-kong', 'shanghai', 'riyadh'}
        for market in markets:
            if market['slug'] not in shared:
                continue
            with self.subTest(market=market['slug']):
                snapshot = json.loads(Path(__file__).with_name(
                    market['evidence_snapshot']).read_text())
                self.assertNotIn('status', snapshot)
                self.assertEqual(snapshot['attestation']['platform'], 'gcp-confidential-space')
                self.assertEqual(snapshot['attestation']['source'],
                                 'https://trustedrouter.com/trust/gcp-release.json')
                for route in snapshot['routes']:
                    self.assertTrue(route['source'].startswith('https://trustedrouter.com/v1/models/'))
                page = render(market, markets, 'test')
                self.assertIn('Shared GCP uptime', page)
                self.assertIn('Canonical API', page)
                self.assertIn('Model Inference', page)
                self.assertIn('Review GCP release', page)
                self.assertNotIn('Regional API', page)
                self.assertNotIn('UAE North', page)
                self.assertNotIn('aws.trustedrouter.com', page)
                self.assertNotIn('azure.trustedrouter.com', page)
                self.assertEqual(page.count('class="service-history"'), 2)
                self.assertEqual(page.count('As of '), 1)
                self.assertLess(page.index('class="trust-intro"'), page.index('class="catalogue'))
                self.assertIn('exchange_market=' + market['slug'], page)

    def test_london_uses_shared_gcp_evidence_and_retains_insurance_workloads(self):
        markets = load_markets()
        london = next(m for m in markets if m['slug'] == 'london')
        page = render(london, markets, 'test')
        self.assertIn('Shared GCP uptime', page)
        self.assertIn('Canonical API', page)
        self.assertIn('Model Inference', page)
        self.assertNotIn('Regional API', page)
        self.assertNotIn('UAE North', page)
        self.assertIn('forms, policies and supporting documents', page)
        self.assertIn('Review GCP release', page)
        self.assertIn('exchange_market=london', page)
        self.assertEqual(page.count('As of '), 1)
        self.assertLess(page.index('class="trust-intro"'), page.index('class="catalogue'))

    def test_dubai_keeps_evidence_in_one_azure_scope(self):
        markets = load_markets()
        dubai = next(m for m in markets if m['slug'] == 'dubai')
        page = render(dubai, markets, 'test')
        self.assertIn('UAE North Gateway (Dubai)', page)
        self.assertIn('Azure uptime', page)
        self.assertIn('https://azure.trustedrouter.com/status', page)
        self.assertIn('https://azure.trustedrouter.com/providers', page)
        self.assertIn('https://trust.trustedrouter.com/trust/azure-release.json', page)
        self.assertIn('Published policy measurement', page)
        self.assertIn('Microsoft Azure Attestation', page)
        self.assertNotIn('GCP Confidential Space', page)
        self.assertNotIn('Published build digest', page)
        self.assertNotIn(self.data['attestation']['image_digest'], page)
        self.assertEqual(page.count('As of '), 1)
        self.assertLess(page.index('class="trust-intro"'), page.index('class="catalogue'))
        self.assertIn('exchange_market=dubai', page)

    def test_tokyo_uses_shared_evidence_without_a_regional_claim(self):
        markets = load_markets()
        tokyo = next(m for m in markets if m['slug'] == 'tokyo')
        page = render(tokyo, markets, 'test')
        self.assertIn('Shared GCP uptime', page)
        self.assertIn('Canonical API', page)
        self.assertIn('Model Inference', page)
        self.assertNotIn('Regional API', page)
        self.assertNotIn('UAE North', page)
        self.assertNotIn('No Tokyo component', page)
        self.assertIn('Review GCP release', page)
        self.assertIn('Not proven by attestation', page)
        self.assertEqual(page.count('As of '), 1)
        self.assertLess(page.index('class="trust-intro"'), page.index('class="catalogue'))

    def test_tokyo_refresh_omits_regional_history_and_rejects_wrong_scope(self):
        endpoints = [{'data': [dict(
            provider=provider, usage_type='Credits', provider_name='Tinfoil',
            pricing=dict(prompt='0.000001', completion='0.000002'),
            trustedrouter=dict(provider_e2ee=True, provider_confidential_compute=True,
                               provider_zero_data_retention=True,
                               privacy_tier_label='Confidential + E2EE'),
        )]} for _, _, provider in ROUTES]
        components = [dict(id=key, name=key, description=key,
                           last_checked_at='2026-09-24T00:00:00Z',
                           uptime_24h_percent=None, history=[])
                      for key in ('canonical_api', 'model_inference', 'us_east4_regional_api')]
        status = {'data': dict(components=components, generated_at='2026-09-24T00:00:00Z')}
        result = project(endpoints, status, '2026-09-24T00:00:00Z',
                         self.data['attestation'], market='tokyo')
        self.assertNotIn('status', result)
        self.assertEqual([c['id'] for c in result['service_history']],
                         ['canonical_api', 'model_inference'])
        london = project(endpoints, status, '2026-09-24T00:00:00Z',
                         self.data['attestation'], market='london')
        self.assertEqual(london, result)
        with self.assertRaisesRegex(ValueError, 'GCP release'):
            project(endpoints, status, '2026-09-24T00:00:00Z',
                    {'platform': 'azure-confidential-containers-sev-snp'}, market='tokyo')
        components.pop(0)
        with self.assertRaisesRegex(ValueError, 'canonical API'):
            project(endpoints, status, '2026-09-24T00:00:00Z',
                    self.data['attestation'], market='tokyo')

    def test_dubai_refresh_rejects_wrong_platform_and_unaccepted_policy(self):
        endpoints = [{'data': [dict(
            provider=provider, usage_type='Credits', provider_name='Tinfoil',
            pricing=dict(prompt='0.000001', completion='0.000002'),
            trustedrouter=dict(provider_e2ee=True, provider_confidential_compute=True,
                               provider_zero_data_retention=True,
                               privacy_tier_label='Confidential + E2EE'),
        )]} for _, _, provider in ROUTES]
        component = dict(id='uaenorth_gateway', name='UAE North Gateway (Dubai)',
                         last_checked_at='2026-09-24T00:00:00Z',
                         uptime_24h_percent=None, sample_count_24h=0, history=[])
        status = {'data': dict(components=[component], generated_at='2026-09-24T00:00:00Z')}
        with self.assertRaisesRegex(ValueError, 'Azure release'):
            project(endpoints, status, '2026-09-24T00:00:00Z',
                    self.data['attestation'], market='dubai')
        release = dict(platform='azure-confidential-containers-sev-snp',
                       accepted_hostdata=[], regions=[dict(
                           attestation_url='https://api-azure.trustedrouter.com/attestation',
                           origin_hostname='quill-enclave-uaenorth.uaenorth.azurecontainer.io',
                           hostdata='a' * 64)])
        with self.assertRaisesRegex(ValueError, 'not accepted'):
            project(endpoints, status, '2026-09-24T00:00:00Z', release, market='dubai')
        status['data']['components'][0]['id'] = 'us_east4_regional_api'
        with self.assertRaises(StopIteration):
            project(endpoints, status, '2026-09-24T00:00:00Z', release, market='dubai')

    def test_history_keeps_failures_and_missing_evidence(self):
        data = copy.deepcopy(self.data)
        data['status']['uptime'] = None
        data['status']['history'] = [
            dict(bucket_start='2026-09-23T00:00:00Z', status='up', sample_count=0, uptime_percent=None),
            dict(bucket_start='2026-09-23T01:00:00Z', status='down', sample_count=10, uptime_percent=0),
            dict(bucket_start='2026-09-23T02:00:00Z', status='degraded', sample_count=10, uptime_percent=90),
        ]
        _, health = render_evidence(data)
        self.assertIn('<strong>—</strong>', health)
        self.assertIn('health-bar unknown', health)
        self.assertIn('health-bar down', health)
        self.assertIn('health-bar degraded', health)
        self.assertNotIn('100.00', health)

    def test_catalogue_escapes_source_and_converts_per_token_prices(self):
        self.data['routes'][0]['label'] = '<script>alert(1)</script>'
        self.data['routes'][0]['pricing'] = dict(prompt='0.000001', completion='0.000003')
        catalogue, _ = render_evidence(self.data)
        self.assertIn('≈ $1.00 / $3.00', catalogue)
        self.assertNotIn('<script>', catalogue)
        self.assertIn('&lt;script&gt;', catalogue)
        self.assertIn('As of', catalogue)
        self.assertEqual(catalogue.count('class="privacy-column"'), 3)
        self.assertNotIn('Compare providers', catalogue)

    def test_no_city_claim_or_unreviewed_regional_mapping(self):
        markets = load_markets()
        for market in markets:
            page = render(market, markets, 'test')
            self.assertEqual('US East Regional API' in page, market['slug'] == 'new-york')
            self.assertNotIn('In-region catalogue', page)
            self.assertNotIn('in-region failover', page)

    def test_refresh_refuses_missing_selected_route(self):
        with self.assertRaises(StopIteration):
            project([{'data': []} for _ in ROUTES], {}, '2026-09-23T00:00:00Z')

    def test_confidential_catalogue_contains_three_distinct_e2ee_models(self):
        self.assertEqual(len({r['model'] for r in self.data['routes']}), 3)
        self.assertEqual(self.data['routes'][0]['privacy'], 'Confidential + E2EE')
        self.assertTrue(all(r['privacy'] == 'Confidential + E2EE' for r in self.data['routes']))

    def test_refresh_rejects_lost_confidentiality(self):
        endpoint = {'provider': 'tinfoil', 'usage_type': 'Credits', 'trustedrouter': {
            'provider_e2ee': False, 'provider_confidential_compute': True,
            'provider_zero_data_retention': True,
        }}
        with self.assertRaises(ValueError):
            project([{'data': [endpoint]}, {'data': []}], {}, '2026-09-23T00:00:00Z')

    def test_evidence_separates_public_record_from_live_verification(self):
        _, evidence = render_evidence(self.data)
        self.assertIn('Published build digest', evidence)
        self.assertIn('Verify live attestation', evidence)
        self.assertIn('Not proven by attestation', evidence)
        self.assertIn('24-hour snapshot', evidence)
        self.assertIn('Model Inference', evidence)
        self.assertIn('Canonical API', evidence)
        self.assertNotIn('ALL SYSTEMS OPERATIONAL', evidence)

    def test_page_order_and_no_new_york_processing_promise(self):
        markets = load_markets()
        ny = next(m for m in markets if m['slug'] == 'new-york')
        page = render(ny, markets, 'test')
        self.assertLess(page.index('class="market-band"'), page.index('id="buyers"'))
        self.assertLess(page.index('class="privacy-band"'), page.index('id="sellers"'))
        self.assertIn('Show your service', page)
        self.assertIn('Make it verifiable', page)
        self.assertIn('Serve real workloads', page)
        self.assertNotIn('served from New York', page)
        self.assertNotIn('inside the same jurisdiction', page)

    def test_ny_evidence_uses_one_shared_date_and_quiet_header(self):
        markets = load_markets()
        ny = next(m for m in markets if m['slug'] == 'new-york')
        page = render(ny, markets, 'test')
        self.assertEqual(page.count('As of '), 1)
        self.assertNotIn('Draft for review', page)
        self.assertNotIn('—', ny['headline'])
        self.assertIn('market-picker', page)
        header = page.split('</header>')[0]
        self.assertNotIn('calendly.com', header)
        self.assertIn('Explore the brochure', header)
