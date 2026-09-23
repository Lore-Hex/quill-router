import copy
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import deploy
from build import build, load_markets, render, tracked_url
from deploy import certificate_requests, domains, publish_certificates, url_map

RIYADH = ["riyadhtokenexchange.com", "www.riyadhtokenexchange.com"]


class FakeCloud:
    """In-memory stand-in for the gcloud calls publish_certificates() makes."""

    def __init__(self, certs, attached):
        self.certs = {name: {"name": name, "managed": {"domains": d}} for name, d in certs.items()}
        self.attached = list(attached)
        self.created = []
        self.updates = 0
        self.hooks = {}  # command -> function run once, right after that command

    def attach(self, name, domains):
        self.certs[name] = {"name": name, "managed": {"domains": domains}}
        self.attached.append(name)

    def __call__(self, *args, read=False):
        command = args[:3]
        result = None
        if command == ("compute", "ssl-certificates", "list"):
            result = copy.deepcopy(list(self.certs.values()))
        elif command == ("compute", "target-https-proxies", "describe"):
            result = {"sslCertificates": [f"https://x/sslCertificates/{n}" for n in self.attached]}
        elif command == ("compute", "ssl-certificates", "create"):
            hosts = next(a for a in args if a.startswith("--domains=")).split("=", 1)[1]
            self.certs[args[3]] = {"name": args[3], "managed": {"domains": hosts.split(",")}}
            self.created.append(args[3])
        elif command == ("compute", "target-https-proxies", "update"):
            names = next(a for a in args if a.startswith("--ssl-certificates=")).split("=", 1)[1]
            self.attached = names.split(",")
            self.updates += 1
        else:
            raise AssertionError(f"unexpected gcloud call: {args}")
        hook = self.hooks.pop(command, None)
        if hook:
            hook(self)
        return result


def production_before_riyadh():
    """13 attached certificates, as in production before Riyadh: 7 unrelated,
    4 positional 16-host groups over the other 29 domains, 2 single-site."""
    hosts = [h for d in domains() for h in (d, "www." + d) if "riyadh" not in h]
    certs = {f"unrelated-{n}": [f"site{n}.example.com"] for n in range(7)}
    for i in range(0, len(hosts), 16):
        certs[f"token-exchange-20260919-{i // 16 + 1}"] = hosts[i : i + 16]
    certs["token-exchange-global-20260920"] = ["thetokenexchange.com", "www.thetokenexchange.com"]
    certs["token-exchange-new-york-20260920"] = ["nytokenexchange.com", "www.nytokenexchange.com"]
    return FakeCloud(certs, list(certs))


class MarketLinkParser(HTMLParser):
    def __init__(self, label="Exchange markets"):
        super().__init__()
        self.label = label
        self.in_markets = False
        self.links = []
        self.current = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "nav" and attributes.get("aria-label") == self.label:
            self.in_markets = True
        if tag == "a" and self.in_markets:
            self.links.append(attributes.get("href"))
            if attributes.get("aria-current") == "page":
                self.current.append(attributes.get("href"))

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_markets = False


class ExchangeTests(unittest.TestCase):
    def test_inventory(self):
        self.assertEqual(len(load_markets()), 13)
        self.assertEqual(len(domains()), 30)
        self.assertIn("thetokenexchange.com", domains())
        self.assertIn("nytokenexchange.com", domains())

    def test_pages(self):
        markets = load_markets()
        for market in markets:
            page = render(market, markets, "test")
            self.assertIn(f'<link rel="canonical" href="https://{market["domain"]}/">', page)
            self.assertIn('id="buyers"', page)
            self.assertIn('id="sellers"', page)
            self.assertIn("regional name identifies", page)
            self.assertIn("provider retention", page)
            self.assertIn("not a securities", page)
            self.assertNotIn("<form", page)
            self.assertNotIn("google-analytics", page)
            self.assertNotIn("$headline", page)

    def test_escape(self):
        market = copy.deepcopy(load_markets()[0])
        market["name"] = '<script>alert("x")</script>'
        self.assertNotIn("<script>alert", render(market, [market], "test"))

    def test_global_page_links_all_regional_exchanges(self):
        markets = load_markets()
        global_market = next(m for m in markets if m["domain"] == "thetokenexchange.com")
        parser = MarketLinkParser()
        parser.feed(render(global_market, markets, "test"))
        expected = [f'https://{market["domain"]}/' for market in markets]
        self.assertCountEqual(parser.links, expected)
        self.assertEqual(len(set(parser.links)), len(markets))
        self.assertEqual(len(markets) - 1, 12)

    def test_city_navigation_precedes_hero_on_every_market(self):
        markets = load_markets()
        city_slugs = {
            "new-york", "chicago", "san-francisco", "london",
            "dubai", "riyadh", "hong-kong", "shanghai", "tokyo",
        }
        expected = [f'https://{m["domain"]}/' for m in markets if m["slug"] in city_slugs]
        for market in markets:
            with self.subTest(market=market["slug"]):
                page = render(market, markets, "test")
                self.assertTrue('aria-label="Exchange cities"' in page, "City navigation missing")
                self.assertLess(page.index('aria-label="Exchange cities"'), page.index('<main'))
                cities = MarketLinkParser("Exchange cities")
                cities.feed(page)
                self.assertEqual(cities.links, expected)
                regions = MarketLinkParser("Exchange regions")
                regions.feed(page)
                self.assertCountEqual(
                    cities.links + regions.links, [f'https://{m["domain"]}/' for m in markets]
                )
                self.assertEqual(cities.current + regions.current, [f'https://{market["domain"]}/'])

    def test_attribution(self):
        market = load_markets()[0]
        url = tracked_url(
            "https://trustedrouter.com/token-exchange", market, "brief", "#enterprise-brief"
        )
        self.assertEqual(urlparse(url).fragment, "enterprise-brief")
        self.assertEqual(parse_qs(urlparse(url).query)["utm_source"], ["thetokenexchange.com"])

    def test_map_preserves_and_is_idempotent(self):
        current = {
            "name": "production",
            "fingerprint": "abc",
            "defaultService": "live",
            "hostRules": [{"hosts": ["trustedrouter.com"], "pathMatcher": "main"}],
            "pathMatchers": [{"name": "main", "defaultService": "live"}],
        }
        result = url_map(current)
        self.assertEqual(result["hostRules"][0], current["hostRules"][0])
        self.assertEqual(result["pathMatchers"][0], current["pathMatchers"][0])
        self.assertEqual(result["defaultService"], "live")
        self.assertEqual(result["fingerprint"], "abc")
        self.assertEqual(url_map(result), result)
        self.assertEqual(len({h for r in result["hostRules"] for h in r["hosts"]}), 61)
        self.assertEqual(len(current["pathMatchers"]), 1)
        for matcher in result["pathMatchers"][1:]:
            if "defaultUrlRedirect" in matcher:
                self.assertFalse(matcher["defaultUrlRedirect"]["stripQuery"])
                self.assertTrue(matcher["defaultUrlRedirect"]["httpsRedirect"])

    def test_certificates_requested_only_for_uncovered_hosts(self):
        hosts = [h for d in domains() for h in (d, "www." + d)]
        # Production before Riyadh: 16-host certificates over the other 29 domains.
        before = [h for h in hosts if "riyadh" not in h]
        attached = [{"managed": {"domains": before[i : i + 16]}} for i in range(0, len(before), 16)]
        requests = certificate_requests(attached, hosts)
        self.assertEqual(
            [batch for _, batch in requests],
            [["riyadhtokenexchange.com", "www.riyadhtokenexchange.com"]],
        )
        attached.append({"managed": {"domains": requests[0][1]}})
        self.assertEqual(certificate_requests(attached, hosts), [])

    def test_certificates_from_scratch_cover_every_host(self):
        hosts = [h for d in domains() for h in (d, "www." + d)]
        requests = certificate_requests([], hosts)
        self.assertEqual([h for _, batch in requests for h in batch], hosts)
        self.assertEqual([len(batch) for _, batch in requests], [16, 16, 16, 12])
        names = [name for name, _ in requests]
        self.assertEqual(len(set(names)), len(names))
        for name in names:
            self.assertRegex(name, r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
        self.assertEqual(certificate_requests([], hosts), requests)

    def test_publish_certificates_adds_riyadh_and_keeps_every_attached_certificate(self):
        cloud = production_before_riyadh()
        before = list(cloud.attached)
        with mock.patch.object(deploy, "gcloud", cloud):
            publish_certificates()
            self.assertEqual(len(cloud.created), 1)
            self.assertEqual(cloud.certs[cloud.created[0]]["managed"]["domains"], RIYADH)
            self.assertEqual(cloud.attached, before + cloud.created)
            publish_certificates()
        self.assertEqual((len(cloud.created), cloud.updates), (1, 1))

    def test_publish_certificates_attaches_an_existing_matching_certificate(self):
        cloud = production_before_riyadh()
        [(name, _)] = certificate_requests([], RIYADH)
        cloud.certs[name] = {"name": name, "managed": {"domains": list(reversed(RIYADH))}}
        with mock.patch.object(deploy, "gcloud", cloud):
            publish_certificates()
        self.assertEqual(cloud.created, [])
        self.assertEqual(cloud.attached[-1], name)

    def test_publish_certificates_refuses_other_domains_under_a_requested_name(self):
        cloud = production_before_riyadh()
        [(name, _)] = certificate_requests([], RIYADH)
        cloud.certs[name] = {"name": name, "managed": {"domains": ["wrong.example.com"]}}
        with mock.patch.object(deploy, "gcloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "exists for other domains"):
                publish_certificates()
        self.assertEqual((cloud.created, cloud.updates), ([], 0))

    def test_publish_certificates_refuses_when_the_proxy_changes_during_creation(self):
        changes = {
            "attached": lambda c: c.attach("attached-meanwhile", ["other.example.com"]),
            "removed": lambda c: c.attached.remove("token-exchange-20260919-4"),
        }
        for label, change in changes.items():
            with self.subTest(label):
                cloud = production_before_riyadh()
                cloud.hooks[("compute", "ssl-certificates", "create")] = change
                expected = production_before_riyadh()
                change(expected)
                with mock.patch.object(deploy, "gcloud", cloud):
                    with self.assertRaisesRegex(RuntimeError, "changed during publish"):
                        publish_certificates()
                self.assertEqual(cloud.updates, 0)
                self.assertEqual(cloud.attached, expected.attached)

    def test_publish_certificates_refuses_when_the_proxy_changes_while_nothing_is_needed(self):
        cloud = production_before_riyadh()
        [(name, _)] = certificate_requests([], RIYADH)
        cloud.attach(name, RIYADH)
        cloud.hooks[("compute", "ssl-certificates", "list")] = lambda c: c.attached.remove(
            "token-exchange-20260919-4"
        )
        with mock.patch.object(deploy, "gcloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "changed during publish"):
                publish_certificates()
        self.assertEqual((cloud.created, cloud.updates), ([], 0))

    def test_publish_certificates_refuses_then_uses_a_certificate_attached_mid_run(self):
        # The hook runs right after the named call returns: after the first proxy
        # read (before the inventory is read), or after the inventory read.
        for after in (("compute", "target-https-proxies", "describe"),
                      ("compute", "ssl-certificates", "list")):
            with self.subTest(after=after[1]):
                cloud = production_before_riyadh()
                cloud.hooks[after] = lambda c: c.attach("riyadh-manual", RIYADH)
                with mock.patch.object(deploy, "gcloud", cloud):
                    with self.assertRaisesRegex(RuntimeError, "changed during publish"):
                        publish_certificates()
                    publish_certificates()
                self.assertEqual(cloud.updates, 0)
                self.assertEqual(cloud.attached[-1], "riyadh-manual")
                self.assertNotIn(cloud.created[0], cloud.attached)

    def test_publish_certificates_refuses_when_a_requested_certificate_changes(self):
        cloud = production_before_riyadh()
        cloud.attached.remove("token-exchange-20260919-3")
        hosts = [h for d in domains() for h in (d, "www." + d)]
        covering = [cloud.certs[n] for n in cloud.attached]
        first, *_, (last, last_hosts) = certificate_requests(covering, hosts)
        cloud.certs[last] = {"name": last, "managed": {"domains": last_hosts}}
        swap = {"name": last, "managed": {"domains": ["wrong.example.com"]}}
        cloud.hooks[("compute", "ssl-certificates", "create")] = lambda c: c.certs.update(
            {last: swap}
        )
        with mock.patch.object(deploy, "gcloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "exists for other domains"):
                publish_certificates()
        self.assertEqual((cloud.created, cloud.updates), ([first[0]], 0))

    def test_publish_certificates_refuses_when_an_attached_certificate_is_replaced(self):
        cloud = production_before_riyadh()
        shrunk = {"name": "token-exchange-20260919-4", "managed": {"domains": ["x.example.com"]}}
        cloud.hooks[("compute", "ssl-certificates", "create")] = lambda c: c.certs.update(
            {shrunk["name"]: shrunk}
        )
        with mock.patch.object(deploy, "gcloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "Attached certificates changed"):
                publish_certificates()
        self.assertEqual(cloud.updates, 0)

    def test_publish_certificates_refuses_past_the_proxy_limit(self):
        cloud = production_before_riyadh()
        cloud.attached += ["extra-1", "extra-2"]
        with mock.patch.object(deploy, "gcloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "Certificate limit reached"):
                publish_certificates()
        self.assertEqual((cloud.created, cloud.updates), ([], 0))

    def test_collision_rejected(self):
        with self.assertRaises(ValueError):
            url_map(
                {"hostRules": [{"hosts": ["nytokenexchange.com"], "pathMatcher": "someone-else"}]}
            )

    def test_build(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build(output)
            self.assertEqual(len(list(output.glob("*/index.html"))), 13)
            for market in load_markets():
                self.assertIn(
                    market["domain"], (output / market["slug"] / "sitemap.xml").read_text()
                )
            self.assertTrue((output / "assets/exchange.webp").is_file())


if __name__ == "__main__":
    unittest.main()
