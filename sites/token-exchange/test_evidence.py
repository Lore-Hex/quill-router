"""The static artifact must never contain supposedly live measured values."""
import copy
import json
import unittest
from pathlib import Path

from build import load_markets, render
from evidence import render_evidence
from refresh_evidence import ROUTES, project


class EvidenceTests(unittest.TestCase):
    def test_every_market_uses_one_live_path_without_snapshot_values(self):
        markets = load_markets()
        self.assertEqual(len(markets), 13)
        for market in markets:
            with self.subTest(market=market['slug']):
                page = render(market, markets, 'test')
                profile = market['slug'] if market['slug'] in ('new-york', 'dubai', 'europe') else 'shared-gcp'
                self.assertIn(f'data-evidence-profile="{profile}"', page)
                self.assertIn('/assets/live-evidence.js?v=test', page)
                self.assertIn('data-live-prices', page)
                self.assertNotIn('99.93', page)
                self.assertNotIn('24-hour snapshot', page)
                self.assertNotIn('As of ', page)
                self.assertNotIn('health-bar up', page)
                self.assertNotIn('≈ $', page)
                self.assertNotIn('unavailable', page)
                self.assertIn('Service status ↗', page)
                self.assertIn('Browse Tinfoil model routes ↗', page)
                self.assertIn('data-live-price-unit hidden', page)
                self.assertIn('https://trustedrouter.com/models?filter=e2e', page)

    def test_source_bindings_and_review_links(self):
        for profile in ('new-york', 'shared-gcp', 'europe', 'dubai'):
            _, health = render_evidence(profile)
            if profile == 'dubai':
                self.assertIn('https://azure.trustedrouter.com/status', health)
                self.assertIn('https://trust.trustedrouter.com/trust/azure-release.json', health)
                self.assertNotIn('GCP uptime', health)
            else:
                self.assertIn('https://trustedrouter.com/status', health)
                self.assertIn('https://trustedrouter.com/trust/gcp-release.json', health)
                self.assertNotIn('azure.trustedrouter.com', health)

    def test_no_snapshot_loaded_by_builder(self):
        source = Path(__file__).with_name('build.py').read_text()
        self.assertNotIn('(HERE / evidence_file).read_text()', source)

    def test_existing_page_structure_preserved(self):
        markets = load_markets()
        for market in markets:
            page = render(market, markets, 'test')
            self.assertLess(page.index('class="trust-intro"'), page.index('class="catalogue'))
            self.assertIn('exchange_market=' + market['slug'], page)
            self.assertNotIn('served from New York', page)


class ArchivedRefreshTests(unittest.TestCase):
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
