"""Approved v2 routes, real download contract and source-backed evidence."""
import unittest

from nyte_v2 import render_nyte


class NyteV2Tests(unittest.TestCase):
    def test_navigation_and_ctas(self):
        for name in ('home', 'api', 'sup'):
            page = render_nyte(name)
            self.assertEqual(page.count('>Confidential API</a>'), 2)
            self.assertNotIn('Plan a pilot', page)
            self.assertNotIn('data-go=', page)
            self.assertNotIn('/confidential-api', page)
            self.assertIn('href="/confidential#start">API quick start</a>', page)
            self.assertNotIn('data-open="pdeck"', page)
            self.assertIn('name="website"', page)
            self.assertIn('action="https://trustedrouter.com/token-exchange/brief"', page)
            self.assertIn('id="m-deck"', page)
            self.assertNotIn('Send the deck', page)
        self.assertEqual(render_nyte('sup').count('href="/providers">Get the provider deck</a>'), 2)

    def test_confidential_evidence_has_no_static_success(self):
        page = render_nyte('api')
        for selector in ('data-evidence-profile="new-york"', 'data-live-prices', 'data-live-attestation', 'data-live-services', 'id="start"'):
            self.assertIn(selector, page)
        self.assertNotIn('100.00%', page)
        self.assertNotIn('>Verified<', page)
        self.assertIn('Published release', page)
        self.assertIn('Browse Tinfoil model routes', page)
        self.assertIn('api.confidential.nytokenexchange.com/v1', page)

    def test_home_counts_are_not_mock_values(self):
        page = render_nyte()
        self.assertNotIn('600+', page)
        self.assertNotIn('90+', page)
        self.assertIn('data-catalog-models', page)
        self.assertNotIn('catalog-source', page)
        self.assertNotIn('data-catalog-models>—', page)
        self.assertNotIn('data-catalog-providers>—', page)
        self.assertIn('Operated by Lore Hex Corp, a Delaware corporation.', page)
        self.assertIn('aria-label="Exchange markets"', page)
        self.assertIn('Email us', page)
        self.assertNotIn('autoplay', page)

    def test_index_uses_the_shared_published_snapshot(self):
        import runpy

        from nyte_v2 import HERE
        data = runpy.run_path(str(HERE.parents[1] / "src/trusted_router/content/token_index.py"))["token_index_context"]()
        page = render_nyte('sup')
        for quote in data['quotes'].values():
            self.assertIn(f"${quote['value']:,}", page)
        self.assertNotIn('{{', page)
