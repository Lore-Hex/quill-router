import unittest

from build import load_markets, render
from nyte_v2 import uses_nyte


class SpotAPITests(unittest.TestCase):
    def test_other_markets_keep_existing_primary_experience(self):
        markets = load_markets()
        for market in markets:
            if not uses_nyte(market):
                page = render(market, markets, "test")
                self.assertNotIn('id="spot-api"', page)
                self.assertNotIn('/assets/spot-api.css', page)
                self.assertIn('href="https://trustedrouter.com/trust">Verify a live route', page)



if __name__ == "__main__":
    unittest.main()
