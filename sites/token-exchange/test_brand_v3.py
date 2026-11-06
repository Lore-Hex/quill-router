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

    def test_listed_pages_exist(self):
        self.assertLessEqual(nyte_v2.BRAND_V3_PAGES, set(PAGES))

    def test_preview_loads_it_last(self):
        with mock.patch.dict(os.environ, {"NYTE_BRAND_V3_PREVIEW": "1"}):
            page = render_nyte("providers")
        self.assertGreater(page.index(SHEET), page.index('/assets/nyte-v2/providers.css'))


if __name__ == "__main__":
    unittest.main()
