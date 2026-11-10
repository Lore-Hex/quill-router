"""The brand v3 sheet loads only on pages switched on in nyte-brand-v3.json."""
import os
import re
import unittest
from unittest import mock

import nyte_v2
from nyte_v2 import PAGES, render_nyte

SHEET = '/assets/nyte-v2/brand-v3.css?v='


class BrandV3Tests(unittest.TestCase):
    def test_off_unless_listed(self):
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": ""}), mock.patch.object(nyte_v2, "BRAND_V3_PAGES", set()):
            self.assertNotIn(SHEET, render_nyte("home"))
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": ""}), mock.patch.object(nyte_v2, "BRAND_V3_PAGES", {"reserved-terms"}):
            self.assertIn(SHEET, render_nyte("reserved-terms"))
            self.assertNotIn(SHEET, render_nyte("home"))

    def test_v3_pages_opt_out_of_automatic_dark_mode(self):
        meta = '<meta name="color-scheme" content="only light">'
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": ""}), mock.patch.object(nyte_v2, "BRAND_V3_PAGES", set()):
            self.assertNotIn(meta, render_nyte("home"))
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            self.assertIn(meta, render_nyte("home"))

    def test_favicon_and_share_image_follow_the_switch(self):
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": ""}), mock.patch.object(nyte_v2, "BRAND_V3_PAGES", set()):
            page = render_nyte("home")
        self.assertNotIn('logos/favicon.svg', page)
        self.assertIn('/assets/og-new-york.png?v=', page)
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            page = render_nyte("home")
        self.assertIn('<link rel="icon" href="/assets/nyte-v2/logos/favicon.svg"', page)
        self.assertIn('/assets/og-v3-new-york.png?v=', page)

    def test_v3_home_keeps_actions_and_city_workloads(self):
        from nyte_v2 import load_markets
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            page = render_nyte("home")
            london = render_nyte("home", market=next(m for m in load_markets() if m["slug"] == "london"))
        self.assertIn('/assets/nyte-v2/home-v3.css?v=', page)
        self.assertIn('class="v3-band" preserveAspectRatio="none"', page)
        for hook in ('id="film"', 'id="filmVid"', 'id="filmPlay"', 'href="/buy"', 'href="/confidential#start"', 'data-catalog-models', 'data-catalog-providers'):
            self.assertIn(hook, page)
        self.assertEqual(page.count('data-open="deck"'), 2)
        # Press quotes name the publication with its logo, as on the live page.
        for logo in ('the-information', 'axios', 'semafor'):
            self.assertIn(f'/assets/nyte-v2/press/{logo}-light.png', page)
        # analytics.js names CTA positions from these classes (hero: class starting "hero", close: .closing).
        self.assertIn('<section class="hero-home v3-hero">', page)
        self.assertIn('<section class="v3-cta closing">', page)
        self.assertIn('<ol class="v3-team-list">', london)
        self.assertNotIn('<div class="card">', london)
        self.assertNotIn('class="tag us"', london)

    def test_v3_confidential_keeps_hooks_and_rollback_body(self):
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            page = render_nyte("api")
        self.assertIn('/assets/nyte-v2/api-v3.css?v=', page)
        self.assertGreater(page.index('/assets/nyte-v2/api-v3.css'), page.index(SHEET))
        self.assertIn('class="v3-band" preserveAspectRatio="none"', page)
        self.assertIn('aria-label="A request stops at a closed gate; the provider beyond stays empty"', page)
        for hook in ('data-evidence-profile="new-york"', 'data-evidence-style="v3"', 'data-live-attestation', 'data-live-services',
                     'data-live-caption', 'data-uptime-label="Uptime, last 24 hours"', 'data-live-checked', 'data-live-prices', 'data-price-layout="table"',
                     'data-live-price-unit', 'id="start"', 'id="place-faq"', 'data-place', 'data-us-only', 'data-open="deck"'):
            self.assertIn(hook, page)
        self.assertEqual(page.count('href="/confidential#start">Start building</a>'), 3)
        self.assertIn('<p class="ev-intro">See for yourself what’s running, and whether it’s up.</p>', page)
        self.assertIn('<h3>What’s running</h3>', page)
        self.assertEqual(page.count('data-live-checked'), 1)
        self.assertIn('alt="Semafor" src="/assets/nyte-v2/press/semafor-light.png"', page)
        # No photos or hand-drawn icons on the v3 page.
        for removed in ('ph-gondola', 'close-photo', 'class="ico"'):
            self.assertNotIn(removed, page)
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": ""}), mock.patch.object(nyte_v2, "BRAND_V3_PAGES", set()):
            legacy = render_nyte("api")
        self.assertNotIn('api-v3.css', legacy)
        self.assertIn('ph-gondola', legacy)
        self.assertNotIn('data-evidence-style', legacy)

    def test_v3_buy_moves_only_the_side_notes(self):
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            page = render_nyte("buy")
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": ""}), mock.patch.object(nyte_v2, "BRAND_V3_PAGES", set()):
            legacy = render_nyte("buy")

        def main(html):
            return html.split('<main id="main">', 1)[1].split('</main>', 1)[0]

        def flow(html):
            return html.split('<div class="buy-workspace" id="buy-flow">', 1)[1].split('</noscript>', 1)[0]

        def words(html):
            return sorted(re.sub(r'<svg.*?</svg>|<[^>]+>', ' ', main(html), flags=re.S).split())

        # Rollback: off the list, the page keeps its aside, collapsed list and no v3 sheets.
        self.assertIn('<aside class="buy-brand" aria-label="About reserved tokens">', legacy)
        self.assertIn('<details class="buy-contract"><summary>What we’ll discuss</summary>', legacy)
        for v3_only in ('buy-v3.css', 'brand-v3.css', 'buy-stage', 'buy-about', 'v3-band'):
            self.assertNotIn(v3_only, legacy)
        # Brand v3: its sheet loads after brand-v3.css; the request flow is byte for byte the same.
        self.assertGreater(page.index('/assets/nyte-v2/buy-v3.css?v='), page.index(SHEET))
        self.assertEqual(flow(page), flow(legacy))
        # The side notes move below the yellow field word for word; the list is shown open.
        about = main(page).split('<section class="buy-about" aria-label="About reserved tokens">', 1)[1]
        for pattern in (r'<div class="buy-notes">.*?</p></div>', r'<dl>.*?</dl>', r'<p class="buy-fine">Card checkout.*?</p>'):
            self.assertIn(re.search(pattern, legacy, re.S)[0], about)
        self.assertIn('<div class="buy-contract"><h2 class="v3-label">What we’ll discuss</h2>', about)
        self.assertNotIn('<details', main(page))
        self.assertEqual(main(page).count('class="v3-band" preserveAspectRatio="none"'), 1)
        self.assertEqual(words(page), words(legacy))
        # analytics.js names positions from hero and closing classes; /buy has neither, so clicks stay "section".
        for markup in (main(page), main(legacy)):
            self.assertNotRegex(markup, r'class="hero|class="[^"]*\b(hero|closing|closing-copy|close-photo|cw-download)\b')

    def test_listed_pages_exist(self):
        self.assertLessEqual(nyte_v2.BRAND_V3_PAGES, set(PAGES))

    def test_preview_loads_it_last(self):
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            page = render_nyte("buy")
        self.assertGreater(page.index(SHEET), page.index('/assets/nyte-v2/buy.css'))

    def test_v3_sell_pages_share_one_sheet_and_keep_rollback_bodies(self):
        bodies = {"sup": "hero-sell", "providers": "hero-prov", "accelerator": "hero-acc"}
        for name, hero in bodies.items():
            with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
                page = render_nyte(name)
            self.assertGreater(page.index('/assets/nyte-v2/sell-v3.css'), page.index(SHEET))
            self.assertIn('class="v3-band" preserveAspectRatio="none"', page)
            self.assertEqual(page.count('class="v3-band"'), 1)
            self.assertNotIn('{{', page)
            # analytics.js names CTA positions from a class starting "hero" and from .closing.
            self.assertIn(f'<section class="{hero} sv3-hero', page)
            for removed in ('class="ph ', 'close-photo', 'pv-lockup', 'var(--mint)', 'providers.css', 'accelerator.css'):
                self.assertNotIn(removed, page)
            with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": ""}), mock.patch.object(nyte_v2, "BRAND_V3_PAGES", set()):
                legacy = render_nyte(name)
            self.assertNotIn('sell-v3.css', legacy)
            self.assertNotIn('sv3-', legacy)
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            sup = render_nyte("sup")
        self.assertIn('id="open-stack"', sup)
        self.assertIn('<section class="v3-cta sv3-cta closing">', sup)
        self.assertEqual(sup.count('<details'), 6)
        self.assertEqual(sup.count('<details open>'), 1)
        # Centred Cream heroes with no listing card or illustration on Suppliers and the accelerator.
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            accelerator = render_nyte("accelerator")
        for page in (sup, accelerator):
            self.assertIn('sv3-hero sv3-hero-c">', page)
            self.assertNotIn('v3-illustration', page)
        self.assertNotIn('The supplier listing above is illustrative.', sup)
        self.assertIn('<section class="v3-cta sv3-cta accelerator-call">', accelerator)
        self.assertEqual(sup.count('class="sv3-ladder"'), 4)
        self.assertIn('<time datetime=', sup)
        self.assertEqual(sup.count('class="idx-change '), 5)


if __name__ == "__main__":
    unittest.main()
