"""The static artifact must never contain supposedly live measured values."""
import copy
import json
import unittest
from pathlib import Path

from build import load_markets, render
from evidence import render_evidence
from nyte_v2 import uses_nyte
from refresh_evidence import ROUTES, project


class EvidenceTests(unittest.TestCase):
    def test_every_market_uses_one_live_path_without_snapshot_values(self):
        markets = load_markets()
        self.assertEqual(len(markets), 13)
        for market in markets:
            if uses_nyte(market):
                continue  # New York v2 has dedicated page and browser coverage.
            with self.subTest(market=market['slug']):
                page = render(market, markets, 'test')
                self.assertIn('data-evidence-profile="shared-gcp"', page)
                self.assertIn('/assets/live-evidence.js?v=test', page)
                self.assertIn('data-live-prices', page)
                self.assertNotIn('99.93', page)
                self.assertNotIn('24-hour snapshot', page)
                self.assertNotIn('As of ', page)
                self.assertNotIn('health-bar up', page)
                self.assertNotIn('≈ $', page)
                self.assertNotIn('unavailable', page)
                self.assertIn('Service status', page)
                self.assertIn('Browse Tinfoil model routes', page)
                self.assertIn('data-live-price-unit hidden', page)
                self.assertIn('https://trustedrouter.com/models?filter=e2e', page)

    def test_source_bindings_and_review_links(self):
        _, health = render_evidence()
        self.assertIn('https://trustedrouter.com/status', health)
        self.assertIn('https://trustedrouter.com/trust/gcp-release.json', health)
        self.assertIn('Shared GCP uptime', health)
        self.assertNotIn('azure.trustedrouter.com', health)

    def test_no_snapshot_loaded_by_builder(self):
        source = Path(__file__).with_name('build.py').read_text()
        self.assertNotIn('(HERE / evidence_file).read_text()', source)

    def test_existing_page_structure_preserved(self):
        markets = load_markets()
        for market in markets:
            if uses_nyte(market):
                continue  # New York v2 has dedicated page and browser coverage.
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
        )]
        return endpoints, {'data': dict(
            components=components, generated_at='2026-09-24T00:00:00Z')}

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
