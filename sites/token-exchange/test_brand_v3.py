"""The brand v3 sheet loads only on pages switched on in nyte-brand-v3.json."""
import os
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
        self.assertNotIn('/assets/nyte-v2/press/', page)
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
                     'data-live-caption', 'data-uptime-label="GCP uptime · 24h"', 'data-live-prices', 'data-price-layout="table"',
                     'data-live-price-unit', 'id="start"', 'id="place-faq"', 'data-place', 'data-us-only', 'data-open="deck"'):
            self.assertIn(hook, page)
        self.assertEqual(page.count('href="/confidential#start">Start building</a>'), 3)
        # No photos, third-party logos or hand-drawn icons on the v3 page.
        for removed in ('ph-gondola', 'close-photo', '/assets/nyte-v2/press/', 'class="ico"'):
            self.assertNotIn(removed, page)
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": ""}), mock.patch.object(nyte_v2, "BRAND_V3_PAGES", set()):
            legacy = render_nyte("api")
        self.assertNotIn('api-v3.css', legacy)
        self.assertIn('ph-gondola', legacy)
        self.assertNotIn('data-evidence-style', legacy)

    def test_listed_pages_exist(self):
        self.assertLessEqual(nyte_v2.BRAND_V3_PAGES, set(PAGES))

    def test_preview_loads_it_last(self):
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            page = render_nyte("providers")
        self.assertGreater(page.index(SHEET), page.index('/assets/nyte-v2/providers.css'))


if __name__ == "__main__":
    unittest.main()
