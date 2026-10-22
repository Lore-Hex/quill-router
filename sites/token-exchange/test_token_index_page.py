import runpy
import tempfile
import unittest
from pathlib import Path

from build import build
from token_index_page import CHART_ASSETS, SOURCE


class TokenIndexPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.output = Path(cls.directory.name)
        build(cls.output)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_same_published_data_and_unmodified_charts(self):
        data = runpy.run_path(str(SOURCE / "content/token_index.py"))["token_index_context"]()
        page = (self.output / "index").read_text()
        self.assertIn(f'${data["quotes"]["ALL"]["value"]:,}', page)
        self.assertIn(f'datetime="{data["as_of_iso"]}"', page)
        self.assertIn(data["as_of"], page)
        for grade in data["grades"]:
            self.assertIn(grade["name"], page)
            self.assertIn(f'${data["quotes"][grade["code"]]["value"]:,}', page)
        for name in CHART_ASSETS:
            self.assertEqual(
                (self.output / "assets/token-index" / name).read_bytes(),
                (SOURCE / "static/token-index" / name).read_bytes(),
            )
        self.assertIn("Provisional. Not for trading.", page)
        self.assertIn('rel="canonical" href="https://nytokenexchange.com/index"', page)
        self.assertFalse((self.output / "index.html").exists())

    def test_index_reuses_nyte_chrome(self):
        import re

        home = (self.output / "new-york/index.html").read_text()
        page = (self.output / "index").read_text()
        for tag in ("header", "footer"):
            pattern = rf"<{tag}\b.*?</{tag}>"
            self.assertEqual(re.search(pattern, home.replace("utm_content=home", "utm_content=index"), re.S).group(), re.search(pattern, page, re.S).group())
        self.assertIn('data-nyte-page="index"', page)
        self.assertIn('/assets/nyte-v2/app.js', page)
        self.assertIn('/assets/nyte-v2/styles.css', page)
        self.assertNotIn('class="index-header', page)
        self.assertNotIn('class="index-footer', page)

    def test_new_york_links_index_and_supplier_stack_only(self):
        page = (self.output / "new-york/index.html").read_text()
        self.assertIn('href="/index">Index</a>', page)
        suppliers = (self.output / "suppliers").read_text()
        self.assertIn("token-exchange-neocloud-in-a-box", suppliers)
        self.assertIn('href="/suppliers"', page)
        self.assertIn('href="/confidential"', page)
        self.assertIn("https://nytokenexchange.com/index", (self.output / "new-york/sitemap.xml").read_text())
        for other in self.output.glob("*/index.html"):
            if other.parent.name != "new-york":
                self.assertNotIn('id="open-stack"', other.read_text())
                self.assertNotIn("https://nytokenexchange.com/index", (other.parent / "sitemap.xml").read_text())


if __name__ == "__main__":
    unittest.main()
