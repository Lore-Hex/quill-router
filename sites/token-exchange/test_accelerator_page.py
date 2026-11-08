"""Planned neocloud reservations, public software and a discussion-only handoff."""
import re
import tempfile
import unittest
from html import unescape
from pathlib import Path
from typing import ClassVar
from urllib.parse import urlparse

from build import build, load_markets
from facilities import facility_cards
from nyte_v2 import render_nyte
from test_buy_page import FACILITY_RATE_COPY, assert_facility_cards
from test_provider_page import PageLinks

HERE = Path(__file__).resolve().parent
LOT_NOTE = 'Each lot is 1 billion tokens delivered over the facility’s first twelve months of operation, not per month.'


def assert_facility_columns(test: unittest.TestCase, markup: str) -> None:
    """The approved facts as a brand v3 DataTable: one row per facility, words unchanged."""
    table = re.findall(r'<table class="sv3-table sv3-fac-table">(.*?)</table>', markup, flags=re.S)
    test.assertEqual(len(table), 1)
    for head in ('Facility', 'Targeted launch', 'Grade', 'Power', 'First offering'):
        test.assertIn(f'>{head}</th>', table[0])
    rows = re.findall(r'<tr>(.*?)</tr>', table[0].split('<tbody>', 1)[1], flags=re.S)
    test.assertEqual(len(rows), 2)
    for row, (facility, name, description, power, launch, lots) in zip(rows, [
        ('solar', 'Solar Powered Datacenter', 'solar-powered, air-cooled', '20 kW', 'February 2027', '1,000'),
        ('underwater', 'Underwater Datacenter', 'underwater', '1 MW', 'August 2027', '50,000'),
    ], strict=True):
        test.assertIn(f'<th scope="row"><b>{name}</b><small>A planned {description} data center.</small></th>', row)
        test.assertNotIn('Planned</span>', row)
        test.assertNotIn('operated by', row)
        test.assertNotIn('B200', row)
        test.assertIn(f'<span class="sv3-ml">Power</span>{power}</td>', row)
        test.assertIn(f'<span class="sv3-ml">Targeted launch</span>{launch}</td>', row)
        test.assertIn(f'<span class="sv3-ml">First offering</span>{lots} lots</td>', row)
        test.assertIn('<span class="v3-chip">B</span>', row)
        test.assertIn('<a href="/index">Professional (DeepSeek-class models)</a>', row)
        test.assertIn(f'<a href="/buy?facility={facility}">Discuss a reservation →</a>', row)
        test.assertNotIn('$', row)


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
        self.assertIn('/assets/nyte-v2/sell-v3.css?', self.page)
        self.assertTrue((self.root / 'assets/nyte-v2/sell-v3.css').is_file())
        # The pre-v3 body and sheet stay for rollback.
        self.assertTrue((self.root / 'assets/nyte-v2/accelerator.css').is_file())
        for market in load_markets():
            sitemap = (self.root / market['slug'] / 'sitemap.xml').read_text()
            self.assertEqual(ROUTE in sitemap, market['slug'] == 'new-york')

    def test_contextual_entry_links(self) -> None:
        buy = (self.root / 'buy').read_text().split('<p class="buy-seller">', 1)[1].split('</p>', 1)[0]
        self.assertIn(f'href="{ROUTE}"', buy)
        suppliers = (self.root / 'suppliers').read_text().split('id="open-stack"', 1)[1].split('</section>', 1)[0]
        self.assertIn(f'href="{ROUTE}"', suppliers)
        self.assertIn(f'href="{STACK}"', suppliers)
        self.assertIn(STACK, self.parser.links)
        self.assertIn('/buy', self.parser.links)
        self.assertIn('/providers', self.parser.links)

    def test_plain_booking_and_no_submission_or_embeds(self) -> None:
        # The only form is the shared buyer deck dialog; the accelerator itself collects nothing.
        self.assertEqual([form['id'] for form in self.parser.forms], ['brochure-form'])
        head, deck = self.page.split('<dialog id="m-deck"', 1)
        body = head + deck.split('</dialog>', 1)[1]
        self.assertNotRegex(body, r'<(?:iframe|input|select|textarea|object|embed|form)\b')
        self.assertNotIn('widget.js', self.page)
        # The same booking link in the hero and the closing section.
        links = [urlparse(link) for link in self.parser.links if 'calendly.com' in link]
        self.assertEqual(len(links), 2)
        for link in links:
            self.assertEqual(link.geturl(), 'https://calendly.com/joseph-perla/15min')
        self.assertNotIn('calendly', ' '.join(self.parser.resources))
        self.assertNotIn('.pdf', ' '.join(self.parser.links + self.parser.resources))
        for body in ('accelerator.html', 'accelerator-v3.html'):
            # Decorative bars and icons are aria-hidden; no copy is hidden.
            self.assertNotIn('hidden', (HERE / 'nyte-v2' / body).read_text().replace('aria-hidden="true"', ''))

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
        section = self.page.split('<section class="sv3-facilities">', 1)[1].split('</section>', 1)[0]
        self.assertIn('<h2>First facilities</h2>', section)
        self.assertIn('Pricing is discussed on the call. Operator names are shared on the call.</p>', section)
        self.assertEqual(section.count('Planned on NVIDIA B200/B300-class GPUs.'), 1)
        assert_facility_columns(self, section)
        self.assertEqual(section.count(LOT_NOTE), 1)
        for facility in ['solar', 'underwater']:
            self.assertIn(f'/buy?facility={facility}', self.parser.links)
        self.assertEqual(section.count(FACILITY_RATE_COPY), 1)
        self.assertIn('Pricing is discussed on the call.', section)
        self.assertNotRegex(section.lower(), r'raise|price=|data-price|usd|guarantee|loan|deposit|\bloi\b|fundrais')
        # The rollback body still renders the shared cards the buy page uses.
        assert_facility_cards(self, facility_cards())

    def test_responsibilities_and_four_steps(self) -> None:
        content = (HERE / 'nyte-v2/accelerator-v3.html').read_text()
        roles = content.split('<section class="sv3-roles', 1)[1].split('</section>', 1)[0]
        for number, responsibility in enumerate(['Power', 'Permits', 'The shell: building and cooling', 'Physical security'], 1):
            self.assertIn(f'<li><span class="n">{number:02d}</span>{responsibility}</li>', roles)
        for claim in ['Run it yourself', 'demand from buyers on the exchange',
                      'Hardware, deployment responsibilities and operating terms are scoped together.']:
            self.assertIn(claim, roles)
        steps = content.split('<ol class="sv3-steps4">', 1)[1].split('</ol>', 1)[0]
        self.assertEqual(steps.count('<li class="bar-'), 4)
        for step in ['Plan the facility.', 'List lots and pre-sell reserved tokens.',
                     'Build and go live.', 'Deliver and settle.']:
            self.assertIn(f'<h3>{step}</h3>', steps)


if __name__ == '__main__':
    unittest.main()
