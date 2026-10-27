"""Approved v2 routes, real download contract and source-backed evidence."""
import unittest

from nyte_v2 import index_panel, render_nyte


class NyteV2Tests(unittest.TestCase):
    def test_evidence_script_is_scoped_to_nyte_release(self):
        import tempfile
        from pathlib import Path

        from nyte_v2 import HERE, build_nyte

        page = render_nyte('api')
        self.assertIn('/assets/nyte-v2/live-evidence.js?v=', page)
        self.assertNotIn('src="/assets/live-evidence.js', page)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build_nyte(output)
            self.assertEqual(
                (output / 'assets/nyte-v2/live-evidence.js').read_bytes(),
                (HERE / 'live-evidence.js').read_bytes(),
            )

    def test_navigation_and_ctas(self):
        for name in ('home', 'api', 'sup', 'providers', 'cowork'):
            page = render_nyte(name)
            self.assertEqual(page.count('>Confidential API</span>'), 1)
            self.assertNotIn('Plan a pilot', page)
            self.assertNotIn('data-go=', page)
            self.assertNotIn('/confidential-api', page)
            self.assertIn('href="/confidential#start">Start building</a>', page)
            self.assertNotIn('data-open="pdeck"', page)
            if name in ('home', 'api'):
                self.assertIn('name="website"', page)
                self.assertIn('action="https://trustedrouter.com/token-exchange/brief"', page)
                self.assertIn('id="m-deck"', page)
            else:
                self.assertNotIn('id="m-deck"', page)
            self.assertNotIn('Send the deck', page)
        self.assertEqual(render_nyte('sup').count('href="/providers">Get the provider deck</a>'), 2)

    def test_api_hero_keeps_full_lede_with_phone_copy_span(self):
        import re

        page = render_nyte('api')
        lede = re.search(r'<p class="lede">(.*?)</p>', page).group(1)
        full_copy = ('For prompts that carry client material, regulated records or your own '
                     'source code. Every request runs on verified confidential hardware, '
                     'encrypted end to end, and is never stored.')
        self.assertEqual(re.sub(r'<[^>]+>', '', lede), full_copy)
        self.assertEqual(re.sub(r'<[^>]+>', '', page).count(full_copy), 1)
        self.assertEqual(lede.count('<span class="d">'), 1)

    def test_api_key_links_use_console_route(self):
        page = render_nyte('api')
        self.assertEqual(page.count('href="https://trustedrouter.com/console/api-keys?'), 2)
        self.assertNotIn('/console/keys', page)

    def test_supplier_price_panel_uses_published_snapshot(self):
        panel = index_panel()
        import runpy
        from datetime import date
        from pathlib import Path

        data = runpy.run_path(str(Path(__file__).resolve().parents[2] / 'src/trusted_router/content/token_index.py'))
        self.assertIn('NYTE Token Index', panel)
        self.assertIn('USD per billion tokens · As of ', panel)
        as_of = date.fromisoformat(data["AS_OF_ISO"]).strftime("%m/%d/%y")
        self.assertIn(f'<time datetime="{data["AS_OF_ISO"]}">{as_of}</time>', panel)
        for quote in data['QUOTES'].values():
            self.assertIn(f'${quote["value"]:,}', panel)
        self.assertNotIn('Illustrative', panel)
        self.assertEqual(panel.count('class="idx-change '), 5)
        for quote in data['QUOTES'].values():
            self.assertIn(f'{quote["change_7d"]:+.1f}% 7d', panel)
        self.assertIn('idx-change up', panel)
        self.assertIn('idx-change down', panel)
        page = render_nyte('sup')
        self.assertIn('href="/index">Explore the index</a>', page)
        self.assertNotIn('Index figures are dated published prices', page)

    def test_supplier_listing_steps(self):
        page = render_nyte('sup')
        for title in ('Listing status', 'Show your service', 'Make it verifiable', 'Serve real workloads'):
            self.assertEqual(page.count(title), 1)
        self.assertNotIn('supplier-art', page)
        self.assertNotIn('vsteps', page)

    def test_supplier_stack_panel(self):
        page = render_nyte('sup')
        for label in ('TrustedRouter runs', 'The inference stack is open source.', 'You run',
                      'Get the open-source stack', 'Operator guide'):
            self.assertEqual(page.count('>' + label + '<'), 1)
        for obsolete in ('oss-layers', 'oss-icon', 'duty'):
            self.assertNotIn(obsolete, page)

    def test_confidential_evidence_has_no_static_success(self):
        page = render_nyte('api')
        for selector in ('data-evidence-profile="new-york"', 'data-live-prices', 'data-live-attestation', 'data-live-services', 'id="start"'):
            self.assertIn(selector, page)
        self.assertNotIn('100.00%', page)
        self.assertNotIn('>Verified<', page)
        self.assertIn('Published release', page)
        self.assertIn('Browse Tinfoil model routes', page)
        self.assertIn('api.confidential.nytokenexchange.com/v1', page)

    def test_quickstart_requires_confidential_routing(self):
        import ast
        import html
        import re

        page = render_nyte('api')
        example = re.search(r'<pre>(.*?)</pre>', page, re.S).group(1)
        example = re.sub(r'</span><span class="l', '</span>\n<span class="l', example)
        code = html.unescape(re.sub(r'<[^>]+>', '', example)).replace('\xa0', ' ')
        calls = [node for node in ast.walk(ast.parse(code)) if isinstance(node, ast.Call)]
        client = next(node for node in calls if isinstance(node.func, ast.Name) and node.func.id == 'OpenAI')
        request = next(node for node in calls if isinstance(node.func, ast.Attribute) and node.func.attr == 'create')
        client_args = {item.arg: item.value for item in client.keywords}
        request_args = {item.arg: item.value for item in request.keywords}
        self.assertEqual(ast.literal_eval(client_args['base_url']), 'https://api.confidential.nytokenexchange.com/v1')
        self.assertEqual(ast.literal_eval(request_args['model']), 'nyte/auto')
        self.assertEqual(ast.literal_eval(request_args['extra_body']), {'provider': {'min_privacy': 'confidential'}})
        self.assertNotIn('NYTE address activation is in progress.', page)

    def test_home_counts_are_not_mock_values(self):
        page = render_nyte()
        self.assertNotIn('600+', page)
        self.assertNotIn('90+', page)
        self.assertIn('data-catalog-models', page)
        self.assertNotIn('catalog-source', page)
        self.assertIn('data-catalog-models>—', page)
        self.assertIn('data-catalog-providers>—', page)
        self.assertNotIn('Operated by Lore Hex Corp, a Delaware corporation.', page)
        self.assertNotIn('footer-product-links', page)
        for label in ('Media kit', 'Privacy', 'Terms', 'Exchange overview'):
            self.assertIn('>' + label + '</a>', page)
        self.assertIn('aria-label="Exchange markets"', page)
        self.assertIn('Email us', page)
        self.assertNotIn('autoplay', page)

    def test_index_uses_the_shared_published_snapshot(self):
        import runpy

        from nyte_v2 import HERE
        data = runpy.run_path(str(HERE.parents[1] / "src/trusted_router/content/token_index.py"))["token_index_context"]()
        page = render_nyte('sup')
        for quote in data['quotes'].values():
            self.assertIn(f"${quote['value']:,}", page)
        self.assertNotIn('{{', page)


class NavigationRegistryTests(unittest.TestCase):
    def test_build_rejects_missing_or_unknown_group(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from nyte_v2 import GROUP_GUIDANCE, PAGES, build_nyte

        for group in (None, "buyers"):
            with self.subTest(group=group), tempfile.TemporaryDirectory() as directory:
                with patch.dict(PAGES, {"example": {"route": "/example", "group": group}}):
                    with self.assertRaises(ValueError) as error:
                        build_nyte(Path(directory))
                    self.assertEqual(str(error.exception), GROUP_GUIDANCE + "\nexample")
        with tempfile.TemporaryDirectory() as directory, patch.dict(PAGES, {"example": {"route": "/example"}}):
            with self.assertRaises(ValueError):
                build_nyte(Path(directory))

    def test_registry_navigation_on_every_shell_page(self):
        import re

        from nyte_v2 import GROUPS, PAGES
        from token_index_page import render_token_index

        for key, entry in PAGES.items():
            if entry.get("builder") in {"press", "anchor"}:
                continue
            page = render_token_index() if key == "index" else render_nyte(key)
            nav = re.search(r'<nav class="nav".*?</nav>', page, re.S).group()
            for group in GROUPS:
                panel = re.search(r'id="nav-' + group + r'">(.*?)</div>', nav, re.S).group(1)
                expected = [p["route"] for p in PAGES.values() if p.get("group") == group and p["label"]]
                self.assertEqual(re.findall(r'<a href="([^"]+)"', panel), expected)
            self.assertNotIn('data-nav=', nav)
            self.assertNotIn('href="/media-kit"', nav)
            self.assertIn('href="/media-kit">Media kit</a>', page)
            self.assertEqual(nav.count('aria-expanded="false"'), 3)

    def test_optional_new_until_and_added_page(self):
        import re
        from unittest.mock import patch

        from nyte_v2 import PAGES, render_nav

        sample = {"route": "/sample", "label": "Sample", "group": "sell",
                  "menu_description": "Sample description"}
        with patch.dict(PAGES, {"sample": sample}):
            nav = render_nav()
            panel = re.search(r'id="nav-sell">(.*?)</div>', nav, re.S).group(1)
            self.assertEqual(panel.count('href="/sample"'), 1)
            item = re.search(r'<a href="/sample".*?</a>', panel).group()
            self.assertNotIn('data-new-until', item)
            self.assertNotIn('>NEW<', item)
            with patch.dict(sample, new_until='2026-11-10'):
                item = re.search(r'<a href="/sample".*?</a>', render_nav()).group()
                self.assertIn('data-new-until="2026-11-10"', item)
                self.assertIn('class="tag cc" hidden>NEW', item)
