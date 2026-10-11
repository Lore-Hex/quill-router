"""The customer demo is available without autoplay or eager video downloads."""

import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path

from build import build, load_markets


class MediaParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.videos = []
        self.sources = []

    def handle_starttag(self, tag, attrs):
        if tag == "video":
            self.videos.append(dict(attrs))
        if tag == "source":
            self.sources.append(dict(attrs))


class HomepageVideoTests(unittest.TestCase):
    def test_all_markets_have_accessible_on_demand_video(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build(root)
            self.assertGreater((root / "assets/nyte-routing-demo.mp4").stat().st_size, 0)
            self.assertGreater((root / "assets/nyte-routing-demo-poster.jpg").stat().st_size, 0)
            for market in load_markets():
                page = (root / market["slug"] / "index.html").read_text()
                parser = MediaParser()
                parser.feed(page)
                self.assertEqual(len(parser.videos), 1)
                video = parser.videos[0]
                self.assertIn("controls", video)
                self.assertIn("playsinline", video)
                self.assertNotIn("autoplay", video)
                self.assertEqual(video["preload"], "none")
                self.assertIn("on-screen captions", video["aria-label"])
                self.assertEqual(video["poster"], "/assets/nyte-routing-demo-poster.jpg")
                self.assertEqual(parser.sources, [{"src": "/assets/nyte-routing-demo.mp4", "type": "video/mp4"}])
                self.assertIn('href="/assets/nyte-routing-demo.mp4"', page)


if __name__ == "__main__":
    unittest.main()
