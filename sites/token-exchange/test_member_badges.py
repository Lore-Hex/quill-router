"""Membership branding must not claim blanket security certification."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from build import build, load_markets


class MemberBadgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.output = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.output.cleanup)
        cls.root = Path(cls.output.name)
        build(cls.root)
        cls.page = (cls.root / "badge").read_text()

    def test_clean_alias_metadata_and_membership_scope(self):
        self.assertEqual(self.page, (self.root / "badge.html").read_text())
        self.assertIn('rel="canonical" href="https://nytokenexchange.com/badge"', self.page)
        for copy in ("not a live membership check", "does not establish membership", "only while your membership is active", "route-specific evidence"):
            self.assertIn(copy, self.page)
        self.assertIn('href="https://trustedrouter.com/badge"', self.page)
        self.assertFalse(list(self.root.rglob("*.pdf")))

    def test_four_accessible_static_badges_and_download_links(self):
        for role in ("provider", "enterprise"):
            for theme in ("dark", "light"):
                name = f"nyte-{role}-{theme}.svg"
                svg = (self.root / "assets" / "badges" / name).read_text()
                parsed = ET.fromstring(svg)  # noqa: S314 - generated locally, never untrusted XML
                self.assertEqual(parsed.attrib["viewBox"], "0 0 320 88")
                self.assertIn(f"NYTE {role} member", svg)
                self.assertIn("not a security certification", svg)
                self.assertNotIn("<script", svg)
                self.assertNotIn("href=", svg)
                self.assertIn(f'/assets/badges/{name}" download', self.page)

    def test_all_markets_link_private_deck_and_badge(self):
        for market in load_markets():
            home = (self.root / market["slug"] / "index.html").read_text()
            self.assertIn('href="/providers"', home)
            self.assertIn('href="/badge"', home)
            sitemap = (self.root / market["slug"] / "sitemap.xml").read_text()
            self.assertEqual("https://nytokenexchange.com/badge" in sitemap, market["slug"] == "new-york")

    def test_default_embed_works_without_javascript(self):
        self.assertIn('id="embed-code"', self.page)
        self.assertIn('src="/assets/badges/nyte-provider-dark.svg"', self.page)
        self.assertIn('aria-live="polite"', self.page)
        js = (self.root / "assets" / "badge.js").read_text()
        self.assertIn("navigator.clipboard.writeText", js)
        self.assertIn("copy manually", js)


if __name__ == "__main__":
    unittest.main()
