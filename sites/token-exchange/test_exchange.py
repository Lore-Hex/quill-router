import copy
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from build import build, load_markets, render, tracked_url
from deploy import domains, url_map


class MarketLinkParser(HTMLParser):
    def __init__(self, label="Exchange markets"):
        super().__init__()
        self.label = label
        self.in_markets = False
        self.links = []
        self.current = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "nav" and attributes.get("aria-label") == self.label:
            self.in_markets = True
        if tag == "a" and self.in_markets:
            self.links.append(attributes.get("href"))
            if attributes.get("aria-current") == "page":
                self.current.append(attributes.get("href"))

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_markets = False


class ExchangeTests(unittest.TestCase):
    def test_inventory(self):
        self.assertEqual(len(load_markets()), 12)
        self.assertEqual(len(domains()), 29)
        self.assertIn("thetokenexchange.com", domains())
        self.assertIn("nytokenexchange.com", domains())

    def test_pages(self):
        markets = load_markets()
        for market in markets:
            page = render(market, markets, "test")
            self.assertIn(f'<link rel="canonical" href="https://{market["domain"]}/">', page)
            self.assertIn('id="buyers"', page)
            self.assertIn('id="sellers"', page)
            self.assertIn("regional name identifies", page)
            self.assertIn("provider retention", page)
            self.assertIn("not a securities", page)
            self.assertNotIn("<form", page)
            self.assertNotIn("google-analytics", page)
            self.assertNotIn("$headline", page)

    def test_escape(self):
        market = copy.deepcopy(load_markets()[0])
        market["name"] = '<script>alert("x")</script>'
        self.assertNotIn("<script>alert", render(market, [market], "test"))

    def test_global_page_links_all_regional_exchanges(self):
        markets = load_markets()
        global_market = next(m for m in markets if m["domain"] == "thetokenexchange.com")
        parser = MarketLinkParser()
        parser.feed(render(global_market, markets, "test"))
        expected = [f'https://{market["domain"]}/' for market in markets]
        self.assertCountEqual(parser.links, expected)
        self.assertEqual(len(set(parser.links)), len(markets))
        self.assertEqual(len(markets) - 1, 11)

    def test_city_navigation_precedes_hero_on_every_market(self):
        markets = load_markets()
        city_slugs = {
            "new-york", "chicago", "san-francisco", "london",
            "dubai", "hong-kong", "shanghai", "tokyo",
        }
        expected = [f'https://{m["domain"]}/' for m in markets if m["slug"] in city_slugs]
        for market in markets:
            with self.subTest(market=market["slug"]):
                page = render(market, markets, "test")
                self.assertTrue('aria-label="Exchange cities"' in page, "City navigation missing")
                self.assertLess(page.index('aria-label="Exchange cities"'), page.index('<main'))
                cities = MarketLinkParser("Exchange cities")
                cities.feed(page)
                self.assertEqual(cities.links, expected)
                regions = MarketLinkParser("Exchange regions")
                regions.feed(page)
                self.assertCountEqual(
                    cities.links + regions.links, [f'https://{m["domain"]}/' for m in markets]
                )
                self.assertEqual(cities.current + regions.current, [f'https://{market["domain"]}/'])

    def test_attribution(self):
        market = load_markets()[0]
        url = tracked_url(
            "https://trustedrouter.com/token-exchange", market, "brief", "#enterprise-brief"
        )
        self.assertEqual(urlparse(url).fragment, "enterprise-brief")
        self.assertEqual(parse_qs(urlparse(url).query)["utm_source"], ["thetokenexchange.com"])

    def test_map_preserves_and_is_idempotent(self):
        current = {
            "name": "production",
            "fingerprint": "abc",
            "defaultService": "live",
            "hostRules": [{"hosts": ["trustedrouter.com"], "pathMatcher": "main"}],
            "pathMatchers": [{"name": "main", "defaultService": "live"}],
        }
        result = url_map(current)
        self.assertEqual(result["hostRules"][0], current["hostRules"][0])
        self.assertEqual(result["pathMatchers"][0], current["pathMatchers"][0])
        self.assertEqual(result["defaultService"], "live")
        self.assertEqual(result["fingerprint"], "abc")
        self.assertEqual(url_map(result), result)
        self.assertEqual(len({h for r in result["hostRules"] for h in r["hosts"]}), 59)
        self.assertEqual(len(current["pathMatchers"]), 1)
        for matcher in result["pathMatchers"][1:]:
            if "defaultUrlRedirect" in matcher:
                self.assertFalse(matcher["defaultUrlRedirect"]["stripQuery"])
                self.assertTrue(matcher["defaultUrlRedirect"]["httpsRedirect"])

    def test_collision_rejected(self):
        with self.assertRaises(ValueError):
            url_map(
                {"hostRules": [{"hosts": ["nytokenexchange.com"], "pathMatcher": "someone-else"}]}
            )

    def test_build(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build(output)
            self.assertEqual(len(list(output.glob("*/index.html"))), 12)
            for market in load_markets():
                self.assertIn(
                    market["domain"], (output / market["slug"] / "sitemap.xml").read_text()
                )
            self.assertTrue((output / "assets/exchange.webp").is_file())


if __name__ == "__main__":
    unittest.main()
