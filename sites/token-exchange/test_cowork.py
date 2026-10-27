"""NYTE Cowork's public page must describe the enforced confidential boundary."""

import tempfile
import unittest
from pathlib import Path

from build import build
from nyte_v2 import render_nyte


class CoworkTests(unittest.TestCase):
    def test_page_assets_downloads_and_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root)
            page = (root / "trusted-cowork").read_text()
            self.assertEqual(page, (root / "trusted-cowork.html").read_text())
            self.assertIn('rel="canonical" href="https://nytokenexchange.com/trusted-cowork"', page)
            for arch in ("arm64", "x64"):
                self.assertIn(f"v0.4.0-alpha.3/Trusted-Cowork-0.4.0-alpha.3-mac-{arch}.dmg", page)
            self.assertIn("v0.4.0-alpha.3", page)
            self.assertNotIn("v0.4.0-alpha.2/", page)
            for text in ("nyte/confidential", "fails closed", "Data collection", "deny", "asks before exposing any data to tool use"):
                self.assertIn(text, page)
            self.assertNotIn("nyte/auto", page)
            self.assertTrue((root / "assets/nyte-v2/cowork.css").is_file())
            self.assertIn("https://nytokenexchange.com/trusted-cowork", (root / "new-york/sitemap.xml").read_text())
            self.assertNotIn("/trusted-cowork", (root / "london/sitemap.xml").read_text())

            self.assertTrue((root / "assets/nyte-v2/photos/cowork-clouds-2400.webp").is_file())
            self.assertIn('data-nyte-page="cowork"', page)
            self.assertNotIn("Operated by Lore Hex", page)
            self.assertNotIn("Illustrative session.", page)
            self.assertIn('class="cw-screenshot"', page)
            self.assertTrue((root / "assets/nyte-v2/photos/cowork-app.png").is_file())
            self.assertRegex(page, r'/assets/nyte-v2/cowork\.css\?v=[0-9a-f]{12}')
            self.assertEqual(page.count('href="https://github.com/Lore-Hex/confidential-cowork-desktop"'), 1)

    def test_shared_navigation(self):
        for name in ("home", "api", "sup", "providers", "cowork"):
            page = render_nyte(name)
            self.assertEqual(page.count('>Trusted Cowork</span>'), 1)
            self.assertLess(page.index('>Confidential API</span>'), page.index('>Trusted Cowork</span>'))
            self.assertLess(page.index('>Trusted Cowork</span>'), page.index('>Token Index</span>'))
