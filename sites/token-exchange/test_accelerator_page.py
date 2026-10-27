"""Planned neocloud reservations, public software and a discussion-only handoff."""
import re
import tempfile
import unittest
from html import unescape
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

from build import build, load_markets
from nyte_v2 import render_nyte
from test_buy_page import FACILITY_RATE_COPY, assert_facility_cards
from test_provider_page import PageLinks

HERE = Path(__file__).resolve().parent
ROUTE = '/neocloud-accelerator'
STACK = 'https://github.com/Lore-Hex/token-exchange-neocloud-in-a-box'


class AcceleratorPageTests(unittest.TestCase):
    output: ClassVar[tempfile.TemporaryDirectory[str]]
    root: ClassVar[Path]
    page: ClassVar[str]
    parser: ClassVar[PageLinks]

    @classmethod
    def setUpClass(cls) -> None:
        cls.output = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.output.cleanup)
        cls.root = Path(cls.output.name)
        build(cls.root)
        cls.page = (cls.root / ROUTE.lstrip('/')).read_text()
        cls.parser = PageLinks()
        cls.parser.feed(cls.page)

    def test_routes_shell_and_new_york_sitemap(self) -> None:
        self.assertEqual(self.page, (self.root / 'neocloud-accelerator.html').read_text())
        self.assertEqual(self.page, render_nyte('accelerator'))
        self.assertIn('data-nyte-page="accelerator"', self.page)
        self.assertIn('<title>Neocloud accelerator | New York Token Exchange</title>', self.page)
        self.assertIn(f'<link rel="canonical" href="https://nytokenexchange.com{ROUTE}">', self.page)
        self.assertIn(f'<meta property="og:url" content="https://nytokenexchange.com{ROUTE}">', self.page)
        self.assertIn('/assets/nyte-v2/styles.css?', self.page)
        self.assertIn('/assets/nyte-v2/accelerator.css?', self.page)
        self.assertTrue((self.root / 'assets/nyte-v2/accelerator.css').is_file())
        for market in load_markets():
            sitemap = (self.root / market['slug'] / 'sitemap.xml').read_text()
            self.assertEqual(ROUTE in sitemap, market['slug'] == 'new-york')

    def test_navigation_and_contextual_entry_links(self) -> None:
        for name in ['new-york/index.html', 'index', 'confidential', 'suppliers',
                     'providers', 'buy', 'neocloud-accelerator']:
            page = (self.root / name).read_text()
            self.assertRegex(page, r'<a href="/buy">Reserved tokens</a>\s*'
                             r'<a href="/neocloud-accelerator" data-nav="accelerator">Neocloud accelerator</a>')
        buy = (self.root / 'buy').read_text().split('<p class="buy-seller">', 1)[1].split('</p>', 1)[0]
        self.assertIn(f'href="{ROUTE}"', buy)
        suppliers = (self.root / 'suppliers').read_text().split('id="open-stack"', 1)[1].split('</section>', 1)[0]
        self.assertIn(f'href="{ROUTE}"', suppliers)
        self.assertIn(f'href="{STACK}"', suppliers)
        self.assertIn(STACK, self.parser.links)
        self.assertIn('/buy', self.parser.links)
        self.assertIn('/providers', self.parser.links)
        self.assertIn("accelerator:'/neocloud-accelerator'", (HERE / 'nyte-v2/app.js').read_text())

    def test_plain_booking_and_no_submission_or_embeds(self) -> None:
        self.assertFalse(self.parser.forms)
        self.assertNotRegex(self.page, r'<(?:iframe|input|select|textarea|object|embed)\b')
        self.assertNotIn('widget.js', self.page)
        links = [urlparse(link) for link in self.parser.links if 'calendly.com' in link]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].geturl(), 'https://calendly.com/joseph-perla/15min')
        self.assertNotIn('calendly', ' '.join(self.parser.resources))
        self.assertNotIn('.pdf', ' '.join(self.parser.links + self.parser.resources))
        self.assertNotIn('hidden', (HERE / 'nyte-v2/accelerator.html').read_text())

    def test_claims_and_rate_scope(self) -> None:
        visible = re.sub(r'<(?:style|script)\b[^>]*>.*?</(?:style|script)>', '', self.page, flags=re.S)
        text = unescape(re.sub(r'<[^>]+>', ' ', visible))
        text = re.sub(r'\s+', ' ', text)
        for claim in [
            'pre-sell reserved tokens from a planned facility to help finance the build',
            'Both facilities are planned with targeted launches; NYTE lists them as reservation requests, not live products.',
            'Pre-selling means capacity reservations for inference service.',
            'This is a request to discuss a reservation, not an order.',
            'Capacity futures and derivatives are planned offerings, not live products.',
            'This is an inference service, not a securities or cryptocurrency exchange.',
            'Apache-2.0 open-source neocloud and inference stack',
            'The software is free to use and pre-alpha.',
            'remotely managed by NYTE',
            'Remote management is an offer to discuss, not a live product.',
            'Confidential serving requires independent hardware and software qualification',
            'metering and billing through TrustedRouter',
        ]:
            self.assertIn(claim, text)
        rate = FACILITY_RATE_COPY
        self.assertEqual(text.count(rate), 1)
        self.assertNotIn('tok/s', text.replace(rate, ''))
        for forbidden in ['guarantee', 'invest', '%', '$', 'SLA']:
            self.assertNotIn(forbidden.lower(), text.lower())
        self.assertNotIn('per month', text.replace('not per month', ''))
        self.assertNotIn('monthly lots', text)
        self.assertNotIn('385', text)

    def test_first_facilities_and_reservation_links(self) -> None:
        section = self.page.split('<section class="accelerator-facilities">', 1)[1].split('</section>', 1)[0]
        self.assertIn('<h2>First facilities</h2>', section)
        assert_facility_cards(self, section)
        for facility in ['greenference', 'submerge']:
            self.assertIn(f'/buy?facility={facility}', self.parser.links)
        self.assertEqual(section.count(FACILITY_RATE_COPY), 1)
        self.assertIn('Pricing is discussed on the call.', section)
        self.assertNotRegex(section.lower(), r'raise|price=|data-price|usd|guarantee|loan|deposit|\bloi\b|fundrais')
        buy = (self.root / 'buy').read_text()
        # Shared card prose must remain byte-for-byte identical on both pages.
        for card in re.findall(r'<article class="facility-card">(.*?)</article>', section, flags=re.S):
            for paragraph in re.findall(r'<p>.*?</p>', card):
                self.assertIn(paragraph, buy)

    def test_responsibilities_and_four_steps(self) -> None:
        content = (HERE / 'nyte-v2/accelerator.html').read_text()
        roles = content.split('<div class="split">', 1)[1].split('</section>', 1)[0]
        for responsibility in ['Power', 'Permits', 'The shell: building and cooling', 'Physical security']:
            self.assertIn(f'<li>{responsibility}</li>', roles)
        for claim in ['Run it yourself', 'demand from buyers on the exchange',
                      'Hardware, deployment responsibilities and operating terms are scoped together.']:
            self.assertIn(claim, roles)
        steps = content.split('<ol>', 1)[1].split('</ol>', 1)[0]
        self.assertEqual(steps.count('<li>'), 4)
        for step in ['Plan the facility.', 'List lots and pre-sell reserved tokens.',
                     'Build and go live.', 'Deliver and settle.']:
            self.assertIn(f'<h3>{step}</h3>', steps)


if __name__ == '__main__':
    unittest.main()
