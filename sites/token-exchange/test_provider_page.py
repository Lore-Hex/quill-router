"""The provider landing page is public; its confidential source deck is not."""

import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from build import build, load_markets


class PageLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.resources = []
        self.forms = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            self.links.append(attrs.get("href", ""))
        if tag in {"iframe", "object", "embed", "img", "script"}:
            self.resources.append(attrs.get("src", attrs.get("data", "")))
        if tag == "form":
            self.forms.append(attrs)


class ProviderPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.output.cleanup)
        cls.root = Path(cls.output.name)
        build(cls.root)
        cls.page = (cls.root / "providers.html").read_text()
        cls.parser = PageLinks()
        cls.parser.feed(cls.page)

    def test_request_only_no_pdf_or_private_slides(self):
        self.assertEqual((self.root / "providers").read_text(), self.page)
        self.assertFalse(list(self.root.rglob("*.pdf")))
        for resource in self.parser.links + self.parser.resources:
            self.assertNotIn(".pdf", resource.lower())
            self.assertNotIn("slide-", resource.lower())
        self.assertIn("Shared privately on request", self.page)
        self.assertIn("No public download", self.page)
        self.assertIn("not guarantees", self.page)
        self.assertFalse(self.parser.forms)

    def test_mailto_request_contains_correct_recipient_and_context(self):
        requests = [urlparse(href) for href in self.parser.links if "?subject=" in href]
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.scheme, "mailto")
        self.assertEqual(request.path, "providers@trustedrouter.com")
        query = parse_qs(request.query)
        self.assertIn("provider deck request", query["subject"][0])
        self.assertIn("Company:", query["body"][0])
        self.assertIn("My role:", query["body"][0])

    def test_metadata_and_new_york_only_sitemap(self):
        url = "https://nytokenexchange.com/providers.html"
        self.assertIn(f'<link rel="canonical" href="{url}">', self.page)
        self.assertIn(f'<meta property="og:url" content="{url}">', self.page)
        self.assertTrue((self.root / "assets" / "archivo.woff2").is_file())
        self.assertTrue((self.root / "assets" / "og-new-york.png").is_file())
        for market in load_markets():
            sitemap = (self.root / market["slug"] / "sitemap.xml").read_text()
            self.assertEqual(url in sitemap, market["slug"] == "new-york")


if __name__ == "__main__":
    unittest.main()
