"""Press downloads stay public, complete, reproducible and separate from decks."""

import json
import re
import struct
import tempfile
import unittest
import xml.etree.ElementTree as ET
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zipfile import ZipFile

from build import build, load_markets
from nyte_v2 import uses_nyte
from press import ASSETS, EDITORIAL_ASSETS, HERE, build_press


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []
        self.downloads = []
        self.ids = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        for key in ("src", "href", "poster"):
            if key in attrs:
                self.urls.append(attrs[key])
        if tag == "a" and "download" in attrs:
            self.downloads.append(attrs["href"])


class PressTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        cls.root = Path(cls.temp.name)
        build(cls.root)
        cls.page = (cls.root / "media-kit").read_text()
        cls.links = Links()
        cls.links.feed(cls.page)

    def test_public_page_metadata_and_alias(self):
        self.assertEqual(self.page, (self.root / "media-kit.html").read_text())
        self.assertIn('<link rel="canonical" href="https://nytokenexchange.com/media-kit">', self.page)
        self.assertIn('<meta property="og:url" content="https://nytokenexchange.com/media-kit">', self.page)
        self.assertNotIn("<form", self.page)
        self.assertNotIn("analytics.js", self.page)
        self.assertEqual(len(self.links.ids), len(set(self.links.ids)))

    def test_all_local_resources_and_anchors_exist(self):
        self.assertGreater(len(self.links.downloads), 15)
        for url in self.links.urls:
            parsed = urlparse(url)
            if url.startswith("/"):
                self.assertTrue((self.root / parsed.path.lstrip("/")).is_file(), url)
            if url.startswith("#"):
                self.assertIn(url[1:], self.links.ids)

    def test_archive_matches_individual_downloads_and_is_deterministic(self):
        assets = self.root / "assets/press"
        zip_path = assets / "nyte-media-kit.zip"
        original = zip_path.read_bytes()
        with ZipFile(zip_path) as archive:
            self.assertIsNone(archive.testzip())
            self.assertIn("README.txt", archive.namelist())
            self.assertIn("nyte-homepage.png", archive.namelist())
            for name in archive.namelist():
                self.assertFalse(Path(name).is_absolute())
                self.assertNotIn("..", Path(name).parts)
                self.assertNotEqual(Path(name).suffix, ".mp4")
                self.assertNotIn("Prompts", name)
                self.assertNotIn("Review", name)
                self.assertEqual(archive.read(name), (assets / name).read_bytes())
            self.assertIn("nyte-brand-guidelines-v1.pdf", archive.namelist())
            self.assertIn("guide/horizontal.pdf", archive.namelist())
            for url in self.links.downloads:
                if url.startswith("/assets/press/") and not url.endswith(".zip"):
                    self.assertIn(url.removeprefix("/assets/press/"), archive.namelist())
        (assets / "private-deck.pdf").write_bytes(b"must not enter the archive")
        build_press(self.root)
        self.assertEqual(zip_path.read_bytes(), original)
        (assets / "private-deck.pdf").unlink()

    def test_logo_formats_and_image_dimensions(self):
        assets = self.root / "assets/press"
        for name, *_ in ASSETS:
            svg = ET.parse(assets / f"{name}.svg")  # noqa: S314 - repository-owned export
            self.assertEqual(svg.getroot().tag, "{http://www.w3.org/2000/svg}svg")
            self.assertFalse(svg.findall(".//{http://www.w3.org/2000/svg}script"))
            self.assertFalse(svg.findall(".//{http://www.w3.org/2000/svg}text"))
        expected = {
            "nyte-full-name-logo.png": (2400, 1000),
            "guide/horizontal.png": (2400, 498), "guide/stacked-lockup.png": (2400, 1924),
            "guide/one-line-name.png": (2400, 240), "guide/stacked-name.png": (2400, 1499),
            "guide/wordmark.png": (2400, 807), "guide/globe.png": (2400, 1225),
            "nyte-homepage.png": (1920, 1080),
        }
        expected.update({name: (1672, 941) for name, _ in EDITORIAL_ASSETS})
        for filename, dimensions in expected.items():
            data = (assets / filename).read_bytes()
            self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
            self.assertEqual(struct.unpack(">II", data[16:24]), dimensions)

    def test_kit_uses_the_nyte_globe_not_the_trustedrouter_mark(self):
        assets = self.root / "assets/press"
        self.assertIn('<h1 id="title">Media kit.</h1>', self.page)
        self.assertIn('src="/assets/press/guide/horizontal.svg"', self.page)
        self.assertNotIn('href="/assets/mark.svg"', self.page)
        self.assertNotIn('src="/assets/mark.svg"', self.page)
        self.assertNotIn("Shared mark", self.page)
        self.assertNotIn("NYTE uses the TrustedRouter routing mark", self.page)
        self.assertEqual(json.loads((assets / "nyte-fact-sheet.json").read_text())["name"], "NY Token Exchange")
        for name, _, _, *formats in ASSETS:
            for ext in formats:
                self.assertEqual((assets / f"{name}.{ext}").read_bytes(), (HERE / f"press-assets/{name}.{ext}").read_bytes())
            root = ET.parse(assets / f"{name}.svg").getroot()  # noqa: S314
            self.assertIn("NY", root.attrib["aria-label"])
            self.assertTrue(root.findall(".//{http://www.w3.org/2000/svg}path"))
            self.assertNotIn("TrustedRouter", ET.tostring(root, encoding="unicode"))
        for name in ("README.txt", "nyte-fact-sheet.txt"):
            self.assertNotIn("mark is shared with TrustedRouter", (assets / name).read_text())
        self.assertIn('content="https://nytokenexchange.com/assets/press/nyte-splash-04-marketplace.png"', self.page)

    def test_combined_logos_include_globe_and_outlined_name_and_appear_first(self):
        assets = self.root / "assets/press"
        self.assertEqual(ASSETS[0][0], "nyte-full-name-logo")
        self.assertEqual(len(ASSETS), 7)
        for name, _, background, *formats in ASSETS:
            self.assertEqual(formats, ["svg", "png", "pdf"])
            root = ET.parse(assets / f"{name}.svg").getroot()  # noqa: S314
            paths = root.findall(".//{http://www.w3.org/2000/svg}path")
            fills = {path.attrib.get("fill", "").lower() for path in paths}
            self.assertEqual(background, "dark" if name == "guide/stacked-name" else "light")
            self.assertEqual(fills, {"#faf8f4" if background == "dark" else "#151311"})
            self.assertTrue((assets / f"{name}.pdf").read_bytes().startswith(b"%PDF-"))

    def test_full_name_logo_preserves_both_supplied_outlines(self):
        root = ET.parse(HERE / "press-assets/nyte-full-name-logo.svg").getroot()  # noqa: S314
        self.assertIn("NY Token Exchange", root.attrib["aria-label"])
        self.assertEqual(root.attrib["viewBox"], "0 0 1200 500")
        for name in ("globe", "one-line-name"):
            source = ET.parse(HERE / f"press-assets/guide/{name}.svg").getroot()  # noqa: S314
            group = root.find(f"{{http://www.w3.org/2000/svg}}g[@id='{name}']")
            self.assertIsNotNone(group)
            self.assertEqual([path.attrib for path in group], [path.attrib for path in source])
        self.assertIn('<h3>NY Token Exchange logo</h3>', self.page)
        self.assertLess(self.page.index('src="/assets/press/nyte-full-name-logo.svg"'),
                        self.page.index('src="/assets/press/guide/one-line-name.svg"'))

    def test_new_design_artwork_and_public_brand_guide(self):
        css = (HERE / "press.css").read_text()
        for color in ("#9B93F5", "#FFC800", "#C9E265", "#BFE3EC"):
            self.assertIn(color, css)
        self.assertNotRegex(css, r"font-size:[^;}]*vw")
        self.assertNotRegex(css, r"letter-spacing:\s*-")
        for name, _ in EDITORIAL_ASSETS:
            self.assertIn(f'/assets/press/{name}', self.page)
        self.assertIn('/assets/press/nyte-brand-guidelines-v1.pdf', self.page)
        self.assertNotIn("Local design edition", self.page)
        self.assertNotIn("Production notes", self.page)
        self.assertNotIn("nyte-horizontal-light.svg", self.page)

    def test_copy_single_source_and_claim_boundaries(self):
        data = json.loads((HERE / "press.json").read_text())
        exported = json.loads((self.root / "assets/press/nyte-fact-sheet.json").read_text())
        self.assertEqual(data, exported)
        text = (self.root / "assets/press/nyte-fact-sheet.txt").read_text()
        for key in ("one_sentence", "boilerplate", "founder_bio"):
            self.assertIn(data[key], self.page)
            self.assertIn(data[key], text)
        for phrase in ("not a securities or cryptocurrency exchange", "not live products", "not an inference location", "Not every provider", "no guaranteed volume"):
            self.assertIn(phrase, self.page)
        self.assertIn("symbolic generated artwork", text)

    def test_every_market_footer_links_to_its_media_kit(self):
        markets = load_markets()
        self.assertEqual(
            {path.parent.name for path in self.root.glob("*/index.html")},
            {market["slug"] for market in markets},
        )
        for market in markets:
            with self.subTest(market=market["slug"]):
                page = (self.root / market["slug"] / "index.html").read_text()
                footer = re.search(r'<footer\b[^>]*>(.*?)</footer>', page, re.S).group(1)
                legal = re.search(r'<div class="legal">(.*?)</div>', footer, re.S).group(1)
                links = re.findall(r'<a\b[^>]*href="([^"]+)"[^>]*>Media kit</a>', legal)
                self.assertEqual(len(links), 1)
                expected = (f"https://{market['domain']}/media-kit" if uses_nyte(market)
                            else "https://trustedrouter.com/media-kit")
                self.assertEqual(urljoin(f'https://{market["domain"]}/', unescape(links[0])), expected)

    def test_new_york_discoverability_only(self):
        for market in load_markets():
            sitemap = (self.root / market["slug"] / "sitemap.xml").read_text()
            homepage = (self.root / market["slug"] / "index.html").read_text()
            self.assertEqual("https://nytokenexchange.com/media-kit" in sitemap, market["slug"] == "new-york")
            if market["slug"] != "new-york":
                self.assertNotIn('href="https://nytokenexchange.com/media-kit"', homepage)


if __name__ == "__main__":
    unittest.main()
