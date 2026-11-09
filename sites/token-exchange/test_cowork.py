"""NYTE Cowork's public page must describe the enforced confidential boundary."""

import tempfile
import unittest
from pathlib import Path

from build import build


class CoworkTests(unittest.TestCase):
    def test_page_assets_downloads_and_canonical(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root)
            page = (root / "trusted-cowork").read_text()
            self.assertEqual(page, (root / "trusted-cowork.html").read_text())
            self.assertIn('rel="canonical" href="https://nytokenexchange.com/trusted-cowork"', page)
            for arch in ("arm64", "x64"):
                self.assertIn(f"v0.4.0-alpha.5/Trusted-Cowork-0.4.0-alpha.5-mac-{arch}.dmg", page)
            self.assertIn("v0.4.0-alpha.5", page)
            self.assertNotIn("v0.4.0-alpha.3", page)
            self.assertNotIn("v0.4.0-alpha.2/", page)
            for text in ("nyte/confidential", "fails closed", "Data collection", "deny", "asks before exposing any data to tool use"):
                self.assertIn(text, page)
            self.assertNotIn("nyte/auto", page)
            self.assertTrue((root / "assets/nyte-v2/cowork.css").is_file())
            self.assertTrue((root / "assets/nyte-v2/cowork-v3.css").is_file())
            self.assertIn("https://nytokenexchange.com/trusted-cowork", (root / "new-york/sitemap.xml").read_text())
            self.assertNotIn("/trusted-cowork", (root / "london/sitemap.xml").read_text())

            self.assertTrue((root / "assets/nyte-v2/photos/cowork-clouds-2400.webp").is_file())
            self.assertIn('data-nyte-page="cowork"', page)
            self.assertNotIn("Operated by Lore Hex", page)
            self.assertNotIn("Illustrative session.", page)
            self.assertIn('class="cw-screenshot"', page)
            self.assertTrue((root / "assets/nyte-v2/photos/cowork-app.png").is_file())
            self.assertRegex(page, r'/assets/nyte-v2/cowork(-v3)?\.css\?v=[0-9a-f]{12}')
            self.assertEqual(page.count('href="https://github.com/Lore-Hex/confidential-cowork-desktop"'), 1)

    def test_v3_body_keeps_hooks_and_loads_after_brand_sheet(self):
        import os
        from unittest import mock

        from nyte_v2 import render_nyte

        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            page = render_nyte("cowork")
        self.assertGreater(page.index("/assets/nyte-v2/cowork-v3.css?v="), page.index("/assets/nyte-v2/brand-v3.css?v="))
        self.assertNotIn("/assets/nyte-v2/cowork.css?v=", page)
        self.assertIn('class="hero-cowork cw-hero', page)
        self.assertIn('class="cw-download', page)
        self.assertIn('class="v3-band" preserveAspectRatio="none"', page)
        self.assertEqual(page.count("Download for Apple silicon</a>"), 2)
        self.assertEqual(page.count("Download for Intel Mac</a>"), 2)
        self.assertEqual(page.count('<details name="cw-faq" open>'), 1)
        self.assertNotIn("[", page.split('<main id="main">')[1].split("</main>")[0])
