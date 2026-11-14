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
            # The buyer deck form is on every page so the menu link opens it in place.
            self.assertIn('name="website"', page)
            self.assertIn('action="https://trustedrouter.com/token-exchange/brief"', page)
            self.assertEqual(page.count('id="m-deck"'), 1)
            # Market menu and footer.
            self.assertEqual(page.count('href="/#deck"'), 2)
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
        for title in ('Show your service', 'Make it verifiable', 'Serve real workloads'):
            self.assertEqual(page.count(title), 1)
        self.assertEqual(page.count('Credentials are shared only through the secure onboarding channel.'), 1)
        self.assertNotIn('supplier-art', page)
        self.assertNotIn('vsteps', page)

    def test_supplier_stack_panel(self):
        page = render_nyte('sup')
        stack = page.split('id="open-stack"', 1)[1].split('</section>', 1)[0]
        for label in ('Get the open-source stack', 'Operator guide'):
            self.assertEqual(stack.count('>' + label + '<'), 1)
        # What the stack contains and who runs what are answered in the questions.
        for answer in ('The inference stack is open source: Apache-2.0 software for building a confidential neocloud',
                       'You run the hardware, model inference and your provider brand. TrustedRouter runs customer access, routing and billing.'):
            self.assertEqual(page.count(answer), 1)
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
        self.assertNotIn('aria-label="Exchange markets"', page)
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

        from nyte_v2 import FOOTER_GROUPS, HEADER_MENUS, MENU_EXTRAS, PAGES
        from token_index_page import render_token_index

        def routes(group):
            listed = []
            for key, p in PAGES.items():
                if p.get("group") == group and p["label"]:
                    listed.append(p["route"])
                    listed += [extra["route"] for after, extra in MENU_EXTRAS.get(group, ()) if after == key]
            return listed

        for key, entry in PAGES.items():
            if entry.get("builder") in {"press", "anchor"}:
                continue
            page = render_token_index() if key == "index" else render_nyte(key)
            nav = re.search(r'<nav class="nav".*?</nav>', page, re.S).group()
            # Header menus: Products (buy) and Market (exchange); sell pages live in the footer.
            for group in HEADER_MENUS:
                items = re.search(r'id="nav-' + group + r'"><div class="wrap"><div class="nav-items">(.*?)</div>', nav, re.S).group(1)
                self.assertEqual([h.split('?', 1)[0] for h in re.findall(r'<a href="([^"]+)"', items)], routes(group))
            for sell in (p["route"] for p in PAGES.values() if p.get("group") == "sell"):
                self.assertNotIn(f'href="{sell}"', nav)
            self.assertNotIn('href="/media-kit"', nav)
            self.assertEqual(nav.count('aria-expanded="false"'), 2)
            self.assertEqual(nav.count('class="nav-prompt"'), 1)
            self.assertIn('href="/media-kit">Media kit</a>', page)
            footer = re.search(r'<footer\b[^>]*>.*?</footer>', page, re.S).group()
            columns = re.findall(r'<div class="footer-col"[^>]*><h2>([^<]+)</h2>(.*?)</div>', footer, re.S)
            self.assertEqual([label for label, _ in columns], list(FOOTER_GROUPS.values()) + ["Trust"])
            for (_, links), group in zip(columns[:len(FOOTER_GROUPS)], FOOTER_GROUPS, strict=True):
                hrefs = [h.split('?', 1)[0] for h in re.findall(r'<a href="([^"]+)"', links)]
                self.assertEqual(hrefs[:len(routes(group))], routes(group))
            self.assertEqual(footer.count('href="https://letsencrypt.ai/">Let\'s Encrypt AI</a>'), 1)
            legal = re.search(r'<div class="legal">(.*?)</div>', footer, re.S).group(1)
            labels = re.findall(r'>([^<>]+)</a>', legal)
            self.assertEqual(labels, (['Trust center'] if key in ('providers', 'badge') else []) + ['Media kit', 'Privacy', 'Terms'])
            related = re.search(r'<nav class="footer-related" aria-label="More from TrustedRouter">(.*?)</nav>', footer, re.S).group(1)
            # Outbound TrustedRouter links carry campaign parameters; compare without them.
            self.assertEqual([(href.split('?', 1)[0], text) for href, text in
                              re.findall(r'<a href="([^"]+)">([^<]+)</a>', related)], [
                ('https://trustedrouter.com/token-exchange', 'Exchange overview'),
                ('https://letsencrypt.ai/', "Let's Encrypt AI"),
            ])
            self.assertLess(footer.index('Powered by'), footer.index('class="footer-related"'))

    def test_added_page_gets_automatic_date_without_changing_test_lists(self):
        import re
        from unittest.mock import patch

        from nyte_v2 import PAGES, render_footer_links, render_nav

        sample = {"route": "/sample", "label": "Sample", "group": "buy",
                  "menu_description": "Sample description"}
        with patch.dict(PAGES, {"sample": sample}), patch('nyte_v2.page_added', return_value='2026-10-20'):
            nav = render_nav()
            self.assertEqual(nav.count('href="/sample"'), 1)
            item = re.search(r'<a href="/sample".*?</a>', nav).group()
            self.assertIn('data-added="2026-10-20"', item)
            self.assertIn('class="tag cc" hidden>NEW', item)
            self.assertEqual(render_footer_links().count('href="/sample"'), 1)
        # A new sell page is listed in the footer, not the header.
        with patch.dict(PAGES, {"sample": {**sample, "group": "sell"}}):
            self.assertNotIn('href="/sample"', render_nav())
            self.assertEqual(render_footer_links().count('href="/sample"'), 1)


    def test_creation_date_uses_real_history(self):
        import os
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path
        from unittest.mock import patch

        from nyte_v2 import page_added

        self.addCleanup(page_added.cache_clear)
        git_executable = shutil.which("git")
        self.assertIsNotNone(git_executable)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def git(*args, day="2026-09-01"):
                return subprocess.run(  # noqa: S603 — fixed test arguments in an isolated repository
                    [git_executable, *args], cwd=root, check=True, capture_output=True, text=True,
                    env={**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@example.com",
                         "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@example.com",
                         "GIT_AUTHOR_DATE": day + "T12:00:00Z", "GIT_COMMITTER_DATE": day + "T12:00:00Z"},
                )

            git("init")
            body = root / "sample.html"
            body.write_text("First version")
            git("add", "sample.html")
            git("commit", "-m", "Add page")
            body.write_text("Updated version")
            git("commit", "-am", "Edit page", day="2026-10-01")
            # Reintroducing the same body must not restart its NEW window either.
            git("rm", "sample.html")
            git("commit", "-m", "Remove page", day="2026-10-02")
            body.write_text("Restored version")
            git("add", "sample.html")
            git("commit", "-m", "Restore page", day="2026-10-03")
            with patch("nyte_v2.HERE", root):
                page_added.cache_clear()
                self.assertEqual(page_added("sample.html"), "2026-09-01")
                (root / "new.html").write_text("Uncommitted preview")
                self.assertEqual(page_added("new.html"), "")
                self.assertEqual(page_added("missing.html"), "")

            shallow = root / "shallow"
            git("clone", "--depth=1", root.as_uri(), str(shallow))
            with patch("nyte_v2.HERE", shallow):
                page_added.cache_clear()
                self.assertEqual(page_added("sample.html"), "")

            with tempfile.TemporaryDirectory() as archive, patch("nyte_v2.HERE", Path(archive)):
                page_added.cache_clear()
                self.assertEqual(page_added("sample.html"), "")

    def test_missing_git_builds_without_new_chip_dates(self):
        from unittest.mock import patch

        from nyte_v2 import page_added, render_nyte

        page_added.cache_clear()
        self.addCleanup(page_added.cache_clear)
        with patch("nyte_v2.shutil.which", return_value=None):
            self.assertEqual(page_added("sample.html"), "")
            page = render_nyte()
            self.assertNotIn("data-added=", page)
            self.assertIn('href="/confidential"', page)
            self.assertIn('class="tag cc" hidden>NEW', page)

    def test_unreadable_history_has_no_creation_date(self):
        from subprocess import CompletedProcess
        from unittest.mock import patch

        from nyte_v2 import page_added

        page_added.cache_clear()
        self.addCleanup(page_added.cache_clear)
        with patch("nyte_v2.shutil.which", return_value="/usr/bin/git"), patch(
            "nyte_v2.subprocess.run",
            side_effect=[CompletedProcess([], 0, "false\n"), CompletedProcess([], 1, "")],
        ):
            self.assertEqual(page_added("sample.html"), "")

    def test_city_home_uses_the_new_york_design_under_its_own_name(self):
        from nyte_v2 import nyte_markets

        for market in nyte_markets():
            page = render_nyte(market=market)
            self.assertIn(f'<link rel="canonical" href="https://{market["domain"]}/">', page)
            self.assertIn(f'<span class="brand-place">{market.get("logo_place", market["region"])}</span>', page)
            self.assertIn(f'<span>{market["name"]}</span></div><nav class="footer-links" aria-label="Site">', page)
            self.assertIn(f'href="https://{market["domain"]}/" aria-current="page"', page)
            self.assertIn(f'utm_source={market["domain"]}', page)
            self.assertIn(f'exchange_market={market["slug"]}', page)
            self.assertNotIn('nytokenexchange.com/"', page.split('class="market-links"')[0])
            # A city name never implies where a route runs, and NYTE is named only at checkout.
            self.assertNotIn('class="tag us"', page)
            self.assertNotIn('New York', page.split('class="market-links"')[0])
            self.assertNotIn('by NYTE', page)
            for card in market["workloads"]:
                self.assertIn(f'<h3>{card["title"]}</h3>', page)

    def test_new_york_pages_link_no_other_market(self):
        from nyte_v2 import PAGES, load_markets, nyte_markets

        others = [m["domain"] for m in load_markets() if m["slug"] != "new-york"]
        for page, entry in PAGES.items():
            if "title" not in entry or "builder" in entry:
                continue  # Rendered outside this shell; the build check covers it.
            with self.subTest(page=page):
                rendered = render_nyte(page)
                self.assertNotIn('class="market-links"', rendered)
                self.assertNotIn('footer-markets', rendered)
                for domain in others:
                    self.assertNotIn(f'href="https://{domain}', rendered)
        # City home pages keep their own footer rail until their domains move.
        for market in nyte_markets():
            self.assertIn('aria-label="Exchange markets"', render_nyte(market=market))

    def test_shared_pages_keep_the_visiting_city_name(self):
        from nyte_v2 import nyte_markets

        home = render_nyte()
        self.assertNotIn('by NYTE', home)
        self.assertIn('<span class="brand-place">New York</span>', home)
        for page in ('buy', 'api', 'sup'):
            rendered = render_nyte(page)
            (self.assertIn if page == 'buy' else self.assertNotIn)("'by NYTE'", rendered)
            self.assertNotIn('"nytokenexchange.com"', rendered)
            for market in nyte_markets():
                self.assertIn(f'"{market["domain"]}":{{"slug":"{market["slug"]}","name":"{market["name"]}"', rendered)
        self.assertIn('data-exchange-name>New York Token Exchange', render_nyte('buy'))
        # The reservations API accepts only New York's origin, so city visitors manage there.
        self.assertIn("location.replace('https://nytokenexchange.com'", render_nyte('manage'))
        self.assertNotIn('location.replace', render_nyte('buy'))
        with self.assertRaises(ValueError):
            render_nyte('buy', market=nyte_markets()[0])
        self.assertIn("dataset.billedBy='New York Token Exchange (NYTE)'", render_nyte('buy'))
