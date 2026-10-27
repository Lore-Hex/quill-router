import re
import tempfile
import unittest
from pathlib import Path

from build import build


class ResaleTests(unittest.TestCase):
    def test_private_holder_page_and_resale_terms(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build(root)
            page = (root / 'manage-reservation').read_text()
            self.assertEqual(page, (root / 'manage-reservation.html').read_text())
            for value in ('Manage my reservation', 'holder-email-form', 'holder-redeem', 'holder-backfill',
                          'no-referrer', 'noindex', '/assets/nyte-v2/manage.js'):
                self.assertIn(value, page)
            script = (root / 'assets/nyte-v2/manage.js').read_text()
            for value in ('location.hash.slice(1)', 'history.replaceState', "credentials:'omit'", 'crypto.randomUUID()', 'textContent', 'nyte.resale_listed', 'nyte.resale_withdrawn'):
                self.assertIn(value, script)
            self.assertNotIn('innerHTML', script)
            self.assertNotIn('localStorage', script)
            self.assertIn('Some lots may be resold reservations', (root / 'buy').read_text())
            terms = (root / 'reserved-tokens-terms').read_text()
            for value in ('2026-10-27-v5', 'first successful-payment confirmation', 'first-listed, first-sold', 'Holders cannot set prices', '0.95', 'NYTE keeps', 'new position', 'Unsold listed lots can be withdrawn'):
                self.assertIn(value, terms)

            plain_terms = re.sub(r"<[^>]+>", "", terms)
            self.assertIn("To request a full refund within 30 days, use Manage my reservation or email enterprise@trustedrouter.com with your position ID or Stripe payment reference.", plain_terms)
            self.assertIn('href="mailto:enterprise@trustedrouter.com"', terms)
