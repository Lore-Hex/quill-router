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
                self.assertIn(f"v0.4.0-alpha.3/Trusted-Cowork-0.4.0-alpha.3-mac-{arch}.dmg", page)
            for text in ("nyte/confidential", "fails closed", "data collection", "deny", "Tools require your approval"):
                self.assertIn(text, page)
            self.assertNotIn("nyte/auto", page)
            self.assertTrue((root / "assets/trusted-cowork-icon.png").is_file())
            self.assertTrue((root / "assets/trusted-cowork.css").is_file())
            self.assertIn("https://nytokenexchange.com/trusted-cowork", (root / "new-york/sitemap.xml").read_text())
            self.assertNotIn("/trusted-cowork", (root / "london/sitemap.xml").read_text())
