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
        self.assertLess(page.index('security-resources'), page.index('id="sellers"'))
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
        self.assertIn('Explore the brief', header)
