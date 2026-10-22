"""Approved v2 routes, real download contract and source-backed evidence."""
import unittest

from nyte_v2 import render_nyte


class NyteV2Tests(unittest.TestCase):
    def test_navigation_and_ctas(self):
        for name in ('home', 'api', 'sup'):
            page = render_nyte(name)
            self.assertEqual(page.count('>Confidential API</a>'), 1)
            self.assertNotIn('Plan a pilot', page)
            self.assertNotIn('data-go=', page)
            self.assertNotIn('/confidential-api', page)
            self.assertIn('href="/confidential#start">Start building</a>', page)
            self.assertNotIn('data-open="pdeck"', page)
            self.assertIn('name="website"', page)
            self.assertIn('action="https://trustedrouter.com/token-exchange/brief"', page)
            self.assertIn('id="m-deck"', page)
            self.assertNotIn('Send the deck', page)
        self.assertEqual(render_nyte('sup').count('href="/providers">Get the provider deck</a>'), 2)

    def test_api_key_links_use_console_route(self):
        page = render_nyte('api')
        self.assertEqual(page.count('href="https://trustedrouter.com/console/api-keys?'), 2)
        self.assertNotIn('/console/keys', page)

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
