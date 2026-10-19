import json
import re
import unittest

from build import load_markets, render


class SpotAPITests(unittest.TestCase):
    def test_new_york_has_working_confidential_request_and_docs(self):
        markets = load_markets()
        market = next(m for m in markets if m["slug"] == "new-york")
        page = render(market, markets, "test")
        self.assertIn('id="spot-api"', page)
        self.assertIn('href="#spot-api">Use the spot exchange', page)
        self.assertIn("https://trustedrouter.com/docs#nyte-api", page)
        self.assertIn("https://api.nytokenexchange.com/v1", page)
        self.assertIn("https://api.confidential.nytokenexchange.com/v1/chat/completions", page)
        self.assertIn("Authorization: Bearer $TR_API_KEY", page)
        body = re.search(r"-d '(\{.*?\})'", page, re.S)
        self.assertIsNotNone(body)
        request = json.loads(body[1])
        self.assertEqual(request["model"], "nyte/auto")
        self.assertEqual(request["provider"], {"min_privacy": "confidential"})
        self.assertIs(request["stream"], True)
        self.assertIn("fail closed", page)

    def test_other_markets_keep_existing_primary_experience(self):
        markets = load_markets()
        for market in markets:
            if market["slug"] != "new-york":
                page = render(market, markets, "test")
                self.assertNotIn('id="spot-api"', page)
                self.assertIn('href="https://trustedrouter.com/trust">Verify a live route', page)


if __name__ == "__main__":
    unittest.main()
