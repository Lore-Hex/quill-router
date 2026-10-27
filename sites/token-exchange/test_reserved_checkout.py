import subprocess
import tempfile
import unittest
from pathlib import Path

from build import build, load_markets

HERE = Path(__file__).resolve().parent


class ReservedCheckoutTests(unittest.TestCase):
    def test_built_terms_panel_alias_sitemap_and_executed_ui(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root)
            terms = (root / 'reserved-tokens-terms').read_text()
            self.assertEqual(terms, (root / 'reserved-tokens-terms.html').read_text())
            for text in ['1 billion tokens', 'AAA, A, B or C', 'January 1, 2027 through December 31, 2027',
                         'Unused tokens expire December 31, 2027', 'full refund any time before January 1, 2027',
                         'TrustedRouter account via the NYTE API', 'price is locked at purchase',
                         'prepaid inference service, not a security or investment', '2026-10-27']:
                self.assertIn(text, terms)
            self.assertIn('/assets/nyte-v2/styles.css', terms)
            self.assertIn('href="https://nytokenexchange.com/reserved-tokens-terms"', terms)
            for market in load_markets():
                sitemap = (root / market['slug'] / 'sitemap.xml').read_text()
                self.assertEqual('/reserved-tokens-terms' in sitemap, market['slug'] == 'new-york')
            page = (root / 'buy').read_text()
            panel = page.split('id="buy-reserve"', 1)[1].split('</section>', 1)[0]
            for text in ['Reserve now by card', '2027 delivery', 'Full refund on request',
                         'Unused tokens expire December 31, 2027', 'href="/reserved-tokens-terms"',
                         'id="buy-reserve-terms" type="checkbox" required']:
                self.assertIn(text, panel)
            result = subprocess.run(['node', str(HERE / 'test-reserve-checkout.cjs'), str(root / 'buy')],  # noqa: S603,S607
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
