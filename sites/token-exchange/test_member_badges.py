"""The Verified Secure badge must bind its claim to eligible enterprise AI routes."""

import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from build import build, load_markets
from member_badges import KIT_BADGES, VERIFIED_SECURE, build_badges
from nyte_v2 import uses_nyte


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
        for copy in ("NYTE requires every AI route", "not a live verification result", "provider-side E2EE", "fail closed", "client TLS terminates inside the attested gateway", "route-specific evidence"):
            self.assertIn(copy, self.page)
        # The shell tags trustedrouter.com links with campaign parameters; targets are unchanged.
        self.assertIn('href="https://trustedrouter.com/badge?', self.page)
        # Only the explicitly public brand guide/logo exports may be published.
        self.assertEqual(
            {p.relative_to(self.root).as_posix() for p in self.root.rglob("*.pdf")},
            {"assets/press/nyte-brand-guidelines-v1.pdf", "assets/press/nyte-full-name-logo.pdf"}
            | {f"assets/press/guide/{name}.pdf" for name in (
                "horizontal", "stacked-lockup", "one-line-name", "stacked-name", "wordmark", "globe",
            )},
        )

    def test_verified_secure_badges_and_download_links(self):
        self.assertNotIn('name="role"', self.page)
        self.assertNotIn('nyte-provider-', self.page)
        self.assertNotIn("Privacy Secured", self.page)
        self.assertIn("<title>NYTE Verified Secure badge | New York Token Exchange</title>", self.page)
        self.assertIn('for enterprise buyers to display to their customers', self.page)
        self.assertIn('Downloading the artwork does not verify your product', self.page)
        for name in VERIFIED_SECURE:
            svg = (self.root / "assets" / "badges" / name).read_text()
            self.assertEqual(svg, (KIT_BADGES / name).read_text())
            self.assertIn('aria-label="NYTE Verified Secure"', svg)
            self.assertNotIn("<script", svg)
            self.assertNotIn("href=", svg)
        # The default (light background) download and the hero use the black kit file.
        self.assertIn('id="download" href="/assets/badges/nyte-verified-secure-black.svg" download', self.page)
        self.assertIn('href="/assets/badges/nyte-verified-secure-white.svg" download', self.page)
        self.assertEqual(self.page.count('alt="NYTE Verified Secure"'), 2)
        self.assertEqual(self.page.count('role="tab"'), 2)
        self.assertEqual(self.page.count('class="v3-band" preserveAspectRatio="none"'), 1)

    def test_earlier_seals_stay_published_for_existing_embeds(self):
        self.assertEqual(len(list((self.root / "assets" / "badges").glob("*.svg"))), 4)
        for theme in ("dark", "light"):
            svg = (self.root / "assets" / "badges" / f"nyte-enterprise-{theme}.svg").read_text()
            parsed = ET.fromstring(svg)  # noqa: S314 - generated locally, never untrusted XML
            self.assertEqual(parsed.attrib["viewBox"], "0 0 320 88")
            self.assertIn("NYTE Privacy Secured", svg)
            self.assertIn("not a product-wide security certification", svg)
            self.assertNotIn("<script", svg)
            self.assertNotIn("href=", svg)

    def test_all_markets_link_private_deck_and_badge(self):
        for market in load_markets():
            if uses_nyte(market):
                self.assertIn('href="/providers"', (self.root / "suppliers").read_text())
                continue  # v2 moves supplier CTAs to /suppliers.
            home = (self.root / market["slug"] / "index.html").read_text()
            self.assertIn('href="/providers"', home)
            self.assertIn('href="/badge"', home)
            buyers = home.split('id="buyers"')[1].split('</section>')[0]
            sellers = home.split('id="sellers"')[1].split('</section>')[0]
            self.assertIn('href="/badge"', buyers)
            self.assertNotIn('href="/badge"', sellers)
            sitemap = (self.root / market["slug"] / "sitemap.xml").read_text()
            self.assertEqual("https://nytokenexchange.com/badge" in sitemap, market["slug"] == "new-york")

    def test_default_embed_works_without_javascript(self):
        self.assertIn('id="embed-code"', self.page)
        embed = self.page.split('<code id="embed-code">', 1)[1].split("</code>", 1)[0]
        self.assertIn("nyte-verified-secure-black.svg", embed)
        self.assertIn("NYTE Verified Secure: requirements and evidence", embed)
        self.assertIn('src="/assets/badges/nyte-verified-secure-black.svg"', self.page)
        self.assertIn('aria-live="polite"', self.page)
        js = (self.root / "assets" / "nyte-v2" / "badge-v3.js").read_text()
        self.assertIn("navigator.clipboard.writeText", js)
        self.assertIn("copy manually", js)
        self.assertIn("/assets/nyte-v2/badge-v3.js?v=", self.page)
        self.assertIn("/assets/nyte-v2/badge-v3.css?v=", self.page)

    def test_rebuild_removes_only_retired_provider_marks(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp)
            for theme in ("dark", "light"):
                (output / f"nyte-provider-{theme}.svg").write_text("old provider badge")
            (output / "unrelated.svg").write_text("preserve unrelated assets")
            build_badges(output)
            self.assertFalse(list(output.glob("nyte-provider-*.svg")))
            self.assertEqual(len(list(output.glob("nyte-enterprise-*.svg"))), 2)
            self.assertEqual(len(list(output.glob("nyte-verified-secure-*.svg"))), 2)
            self.assertEqual((output / "unrelated.svg").read_text(), "preserve unrelated assets")


if __name__ == "__main__":
    unittest.main()
