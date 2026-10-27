import copy
import json
import re
import tempfile
import unittest
import urllib.error
import xml.etree.ElementTree as ET
from collections.abc import Callable, Collection
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qs, urlparse
from urllib.request import Request

import deploy
from build import PROVIDERS, build, load_markets, render, tracked_url
from deploy import certificate_requests, domains, publish_certificates, unrouted_hosts

RIYADH = ["riyadhtokenexchange.com", "www.riyadhtokenexchange.com"]
REAL_BROCHURE_GAPS = deploy.brochure_gaps
REAL_REQUIRE_NYTE_CATALOGUE = deploy.require_nyte_catalogue


class FakeCloud:
    """In-memory stand-in for the gcloud calls publish_certificates() makes."""

    def __init__(self, certs: dict[str, list[str]], attached: list[str]) -> None:
        self.certs: dict[str, Any] = {name: {"name": name, "managed": {"domains": d}} for name, d in certs.items()}
        self.attached = list(attached)
        self.created: list[str] = []
        self.updates = 0
        self.hooks: dict[tuple[str, ...], Callable[[FakeCloud], None]] = {}  # command -> function run once, right after that command
        self.certificate_map: str | None = None
        self.map_entries: dict[str, list[str]] = {}  # hostname -> certificate names
        self.map_entry_states: dict[str, str] = {}  # hostname -> entry state; ACTIVE when absent
        self.map_states: dict[str, str] = {}  # certificate name -> managed.state
        self.calls: list[tuple[str, ...]] = []
        self.unrouted: Collection[str] = ()

    def attach(self, name: str, domains: list[str]) -> None:
        self.certs[name] = {"name": name, "managed": {"domains": domains}}
        self.attached.append(name)

    def __call__(self, *args: str, read: bool = False) -> Any:
        self.calls.append(args)
        command = args[:3]
        result: Any = None
        if command == ("compute", "ssl-certificates", "list"):
            result = copy.deepcopy(list(self.certs.values()))
        elif command == ("compute", "target-https-proxies", "describe"):
            result = {"sslCertificates": [f"https://x/sslCertificates/{n}" for n in self.attached]}
            if self.certificate_map:
                result["certificateMap"] = self.certificate_map
        elif command == ("certificate-manager", "maps", "entries"):
            result = [{"matcher": "PRIMARY", "certificates": ["primary"]}]
            result += [
                {"hostname": h, "certificates": list(c), "state": self.map_entry_states.get(h, "ACTIVE")}
                for h, c in self.map_entries.items()
            ]
        elif command == ("certificate-manager", "certificates", "list"):
            result = [{"name": n, "managed": {"state": s}} for n, s in self.map_states.items()]
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


class PublishCloud(FakeCloud):
    """FakeCloud plus the storage, backend-bucket and URL-map calls publish() makes."""

    def __call__(self, *args: str, read: bool = False) -> Any:
        replies: dict[tuple[str, ...], Any] = {
            ("storage", "buckets", "list"): [{"name": deploy.BUCKET}],
            ("compute", "backend-buckets", "list"): [{"name": deploy.BACKEND}],
            ("compute", "url-maps", "describe"): routed_map(getattr(self, "unrouted", ())),
        }
        writes = {("compute", "backend-buckets", "update")}
        if args[:3] in replies or args[:3] in writes or args[:2] == ("storage", "rsync"):
            self.calls.append(args)
            return copy.deepcopy(replies.get(args[:3]))
        return super().__call__(*args, read=read)


def routed_map(without: Collection[str] = ()) -> dict[str, Any]:
    """The live map as Terraform renders it from markets.json: a matcher for
    each market's domain and one for its aliases; `without` names hosts left
    out."""
    rules = []
    for market in load_markets():
        name = "exchange-" + market["slug"]
        aliases = ["www." + market["domain"], *market["aliases"], *["www." + a for a in market["aliases"]]]
        rules.append({"hosts": [h for h in [market["domain"]] if h not in without], "pathMatcher": name})
        rules.append({"hosts": [h for h in aliases if h not in without], "pathMatcher": name + "-aliases"})
    return {
        "name": deploy.MAP,
        "fingerprint": "abc",
        "hostRules": [{"hosts": ["trustedrouter.com"], "pathMatcher": "main"}, *rules],
        "pathMatchers": [{"name": "main", "defaultService": "live"}],
    }


def with_certificate_map(cloud: FakeCloud, states: dict[str, str] | None = None) -> FakeCloud:
    """Give the proxy the certificate map infra defines: <d> and *.<d> per domain."""
    cloud.certificate_map = (
        "//certificatemanager.googleapis.com/projects/p/locations/global/certificateMaps/control"
    )
    for d in domains():
        certificate = f"projects/p/locations/global/certificates/control-{d.replace('.', '-')}"
        cloud.map_entries[d] = cloud.map_entries[f"*.{d}"] = [certificate]
        cloud.map_states[certificate] = (states or {}).get(d, "ACTIVE")
    return cloud


def built_output(root: Path) -> Path:
    """The files publish() requires before it changes anything."""
    for market in load_markets():
        (root / market["slug"]).mkdir(parents=True, exist_ok=True)
        (root / market["slug"] / "index.html").write_text("page")
        (root / "assets").mkdir(parents=True, exist_ok=True)
        (root / "assets" / f"og-{market['slug']}.png").write_bytes(b"image")
    return root


def production_before_riyadh(cloud_class: type[FakeCloud] | None = None) -> FakeCloud:
    """13 attached certificates, as in production before Riyadh: 7 unrelated,
    4 positional 16-host groups over the other 29 domains, 2 single-site."""
    hosts = [h for d in domains() for h in (d, "www." + d) if "riyadh" not in h]
    certs = {f"unrelated-{n}": [f"site{n}.example.com"] for n in range(7)}
    for i in range(0, len(hosts), 16):
        certs[f"token-exchange-20260919-{i // 16 + 1}"] = hosts[i : i + 16]
    certs["token-exchange-global-20260920"] = ["thetokenexchange.com", "www.thetokenexchange.com"]
    certs["token-exchange-new-york-20260920"] = ["nytokenexchange.com", "www.nytokenexchange.com"]
    return (cloud_class or FakeCloud)(certs, list(certs))


class MarketLinkParser(HTMLParser):
    def __init__(self, label: str = "Exchange markets") -> None:
        super().__init__()
        self.label = label
        self.in_markets = False
        self.links: list[str | None] = []
        self.current: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "nav" and attributes.get("aria-label") == self.label:
            self.in_markets = True
        if tag == "a" and self.in_markets:
            self.links.append(attributes.get("href"))
            if attributes.get("aria-current") == "page":
                self.current.append(attributes.get("href"))

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav":
            self.in_markets = False


class ExchangeTests(unittest.TestCase):
    def setUp(self) -> None:
        # Publish asks the live app whether it accepts every domain; tests stay offline.
        patcher = mock.patch.object(deploy, "brochure_gaps", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        catalogue = mock.patch.object(deploy, "require_nyte_catalogue")
        catalogue.start()
        self.addCleanup(catalogue.stop)

    def test_inventory(self) -> None:
        self.assertEqual(len(load_markets()), 13)
        self.assertEqual(len(domains()), 30)
        self.assertIn("thetokenexchange.com", domains())
        self.assertIn("nytokenexchange.com", domains())

    def test_every_market_domain_has_a_control_certificate(self) -> None:
        # Rule: every market domain is listed for infra/control_lb_certificates.tf.
        listed = json.loads(
            (Path(__file__).parents[2] / "infra" / "token_exchange_certificate_domains.json").read_text()
        )
        self.assertEqual(len(listed), len(set(listed)))
        self.assertEqual(sorted(set(domains()) - set(listed)), [])

    def test_every_market_domain_may_request_the_brochure(self) -> None:
        # Rule: the app allowlists exactly the canonical market domains for the brochure form.
        listed = json.loads(
            (Path(__file__).parents[2] / "src" / "trusted_router" / "data" / "token_exchange_origins.json").read_text()
        )
        self.assertEqual(sorted(listed), sorted(m["domain"] for m in load_markets()))

    def test_publish_refuses_until_the_app_accepts_every_domain(self) -> None:
        with mock.patch.object(deploy, "brochure_gaps", return_value=["newcitytokenexchange.com"]), \
                tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(deploy, "gcloud", side_effect=AssertionError("published")):
            with self.assertRaisesRegex(RuntimeError, "does not accept newcitytokenexchange.com[.] .*deploy the app first"):
                deploy.publish(built_output(Path(directory) / "out"), Path(directory) / "state")

    def test_brochure_gaps_lists_domains_the_live_preflight_rejects(self) -> None:
        def answer(request: Request, timeout: float) -> Any:
            origin = request.headers["Origin"]
            if origin == "https://tokyotokenexchange.com":
                raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", Message(), None)
            if origin == "https://dubaitokenexchange.com":
                raise TimeoutError("read timed out")
            response = mock.MagicMock()
            response.__enter__.return_value.headers = {"Access-Control-Allow-Origin": origin}
            return response

        with mock.patch.object(deploy.urllib.request, "urlopen", answer):
            self.assertEqual(REAL_BROCHURE_GAPS(), ["dubaitokenexchange.com", "tokyotokenexchange.com"])

    def test_nyte_catalogue_requires_cors_and_usable_public_data(self) -> None:
        from io import BytesIO

        class CatalogueResponse(BytesIO):
            headers: dict[str, str | None]

        origin = "https://nytokenexchange.com"
        payloads = {"models/count": {"data": {"count": 2}}, "providers": {"data": [
            {"id": "live", "routing_status": "active"},
            {"id": "blocked", "routing_status": "blocked"},
        ]}}

        def answer(request: Request, timeout: float) -> Any:
            self.assertEqual(request.get_method(), "GET")
            self.assertEqual(request.headers["Origin"], origin)
            self.assertEqual(timeout, 15)
            response = CatalogueResponse(json.dumps(payloads[request.full_url.split("/v1/")[1]]).encode())
            response.headers = {"Access-Control-Allow-Origin": _allowed}
            return response

        _allowed: str | None
        for _allowed in ("*", origin):
            with mock.patch.object(deploy.urllib.request, "urlopen", answer):
                REAL_REQUIRE_NYTE_CATALOGUE()
        for _allowed in (None, "https://unrelated.example"):
            with mock.patch.object(deploy.urllib.request, "urlopen", answer):
                with self.assertRaisesRegex(RuntimeError, "models/count.*CORS support first"):
                    REAL_REQUIRE_NYTE_CATALOGUE()
        _allowed = "*"
        for path, invalid in (("models/count", {"count": True}),
                              ("models/count", {"count": -1}),
                              ("models/count", {"count": "2"}),
                              ("providers", [{"id": "live"}]),
                              ("providers", [{"id": "live", "routing_status": "unknown"}])):
            with self.subTest(path=path, invalid=invalid), mock.patch.dict(payloads, {path: {"data": invalid}}):
                with mock.patch.object(deploy.urllib.request, "urlopen", answer):
                    with self.assertRaisesRegex(RuntimeError, "browser-ready"):
                        REAL_REQUIRE_NYTE_CATALOGUE()
        for failure in (TimeoutError("timed out"), ValueError("invalid JSON")):
            with mock.patch.object(deploy.urllib.request, "urlopen", side_effect=failure):
                with self.assertRaisesRegex(RuntimeError, "browser-ready"):
                    REAL_REQUIRE_NYTE_CATALOGUE()

    def test_publish_refuses_before_cloud_changes_if_catalogue_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(deploy, "require_nyte_catalogue", side_effect=RuntimeError("catalogue unavailable")), \
                mock.patch.object(deploy, "gcloud", side_effect=AssertionError("published")):
            with self.assertRaisesRegex(RuntimeError, "catalogue unavailable"):
                deploy.publish(built_output(Path(directory) / "out"), Path(directory) / "state")

    def test_pages(self) -> None:
        markets = load_markets()
        for market in markets:
            if market["slug"] == "new-york":
                continue  # New York v2 has dedicated page and browser coverage.
            page = render(market, markets, "test")
            self.assertIn(f'<link rel="canonical" href="https://{market["domain"]}/">', page)
            self.assertIn('id="buyers"', page)
            self.assertIn('id="sellers"', page)
            self.assertIn("regional name identifies", page)
            self.assertIn("provider retention", page)
            self.assertIn("not a securities", page)
            # The brochure form is the one form on the page. It posts to TrustedRouter's
            # email-gated download with the honeypot, and every brochure button jumps to it.
            self.assertEqual(page.count("<form"), 1)
            self.assertIn('<form id="brochure-form" action="https://trustedrouter.com/token-exchange/brief" method="post">', page)
            self.assertIn('name="website"', page)
            self.assertEqual(page.count('href="#brochure"'), 3)
            self.assertIn("13-page buyer deck", page)
            self.assertIn("Download the buyer deck", page)
            self.assertNotIn("nine-page", page)
            self.assertNotIn("calendly", page)
            self.assertNotIn("google-analytics", page)
            self.assertIn('<script src="/assets/analytics.js?v=test" defer></script>', page)
            self.assertNotIn("$headline", page)
            # The hero photograph and the provider marquee: one visible provider group,
            # one hidden copy that stays out of the tab order, under the relationship label.
            self.assertIn('class="hero-photo"', page)
            self.assertIn("Providers on TrustedRouter", page)
            self.assertEqual(page.count('<div class="hero-provider-group">'), 1)
            self.assertEqual(page.count('<div class="hero-provider-group" aria-hidden="true">'), 1)
            self.assertEqual(page.count('href="https://trustedrouter.com/providers" tabindex="-1"'), len(PROVIDERS))
            for _, label in PROVIDERS:
                self.assertEqual(page.count(f'<span>{label}</span>'), 2)

    def test_databricks_is_second_in_the_provider_strip(self) -> None:
        self.assertEqual(PROVIDERS[:2], [("openai", "OpenAI"), ("databricks", "Databricks")])
        markets = load_markets()
        for market in markets:
            if market["slug"] == "new-york":
                continue  # New York v2 has dedicated page and browser coverage.
            page = render(market, markets, "test")
            visible = page.split('<div class="hero-provider-group">', 1)[1].split('</div>', 1)[0]
            self.assertLess(visible.index('<span>OpenAI</span>'), visible.index('<span>Databricks</span>'))
            self.assertLess(visible.index('<span>Databricks</span>'), visible.index('<span>Anthropic</span>'))
            self.assertIn('src="/assets/provider-databricks.png"', visible)

    def test_escape(self) -> None:
        market = copy.deepcopy(load_markets()[0])
        market["name"] = '<script>alert("x")</script>'
        self.assertNotIn("<script>alert", render(market, [market], "test"))

    def test_global_page_links_all_regional_exchanges(self) -> None:
        markets = load_markets()
        global_market = next(m for m in markets if m["domain"] == "thetokenexchange.com")
        parser = MarketLinkParser()
        parser.feed(render(global_market, markets, "test"))
        expected = [f'https://{market["domain"]}/' for market in markets]
        self.assertCountEqual(parser.links, expected)
        self.assertEqual(len(set(parser.links)), len(markets))
        self.assertEqual(len(markets) - 1, 12)

    def test_unified_market_navigation_precedes_hero_and_matches_footer(self) -> None:
        markets = load_markets()
        expected = [f'https://{m["domain"]}/' for m in markets]
        for market in markets:
            if market["slug"] == "new-york":
                continue  # New York v2 has dedicated page and browser coverage.
            with self.subTest(market=market["slug"]):
                page = render(market, markets, "test")
                self.assertLess(page.index('aria-label="Market directory"'), page.index('<main'))
                for label in ("Market directory", "Exchange markets"):
                    navigation = MarketLinkParser(label)
                    navigation.feed(page)
                    self.assertEqual(navigation.links, expected)
                    self.assertEqual(navigation.current, [f'https://{market["domain"]}/'])

    def test_nyte_navigation_preserves_all_routes(self) -> None:
        from nyte_v2 import render_nyte
        from token_index_page import render_token_index

        expected = ["/confidential", "/trusted-cowork", "/index", "/buy",
                    "/neocloud-accelerator", "/suppliers", "/providers"]
        for page in ("home", "api", "cowork", "index", "buy", "accelerator", "sup", "providers"):
            with self.subTest(page=page):
                navigation = MarketLinkParser("Main")
                navigation.feed(render_token_index() if page == "index" else render_nyte(page))
                self.assertEqual(navigation.links, expected)

    def test_attribution(self) -> None:
        market = load_markets()[0]
        query = parse_qs(urlparse(tracked_url("https://trustedrouter.com/providers/marketplace", market, "seller")).query)
        self.assertEqual(query["utm_source"], ["thetokenexchange.com"])
        self.assertEqual(query["utm_campaign"], ["token-exchange-launch"])
        self.assertEqual(query["utm_content"], ["seller"])

    def test_publish_checks_routing_and_writes_none(self) -> None:
        # Rule: routing is Terraform's (infra/control_lb_url_map.tf reads
        # markets.json); publish checks every host is routed and never
        # imports a map.
        self.assertEqual(unrouted_hosts(routed_map()), [])
        self.assertEqual(
            unrouted_hosts(routed_map(without={"nytokenexchange.com", "www.nytoken.exchange"})),
            ["nytokenexchange.com", "www.nytoken.exchange"],
        )
        # A host routed by another matcher is not routed for the exchange.
        other = routed_map(without={"nytokenexchange.com"})
        other["hostRules"].append({"hosts": ["nytokenexchange.com"], "pathMatcher": "someone-else"})
        self.assertEqual(unrouted_hosts(other), ["nytokenexchange.com"])
        self.assertEqual(len({h for r in routed_map()["hostRules"][1:] for h in r["hosts"]}), 60)

    def test_publish_refuses_an_unrouted_host(self) -> None:
        cloud = with_certificate_map(production_before_riyadh(PublishCloud))
        cloud.unrouted = {"riyadhtokenexchange.com"}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(deploy, "gcloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "Not routed .*riyadhtokenexchange.com"):
                deploy.publish(built_output(Path(directory) / "out"), Path(directory) / "state")
        commands = [c[:3] for c in cloud.calls]
        self.assertNotIn(("compute", "url-maps", "import"), commands)
        self.assertNotIn(("compute", "ssl-certificates", "create"), commands)

    def test_certificates_requested_only_for_uncovered_hosts(self) -> None:
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

    def test_certificates_from_scratch_cover_every_host(self) -> None:
        hosts = [h for d in domains() for h in (d, "www." + d)]
        requests = certificate_requests([], hosts)
        self.assertEqual([h for _, batch in requests for h in batch], hosts)
        self.assertEqual([len(batch) for _, batch in requests], [16, 16, 16, 12])
        names = [name for name, _ in requests]
        self.assertEqual(len(set(names)), len(names))
        for name in names:
            self.assertRegex(name, r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
        self.assertEqual(certificate_requests([], hosts), requests)

    def test_publish_certificates_adds_riyadh_and_keeps_every_attached_certificate(self) -> None:
        cloud = production_before_riyadh()
        before = list(cloud.attached)
        with mock.patch.object(deploy, "gcloud", cloud):
            publish_certificates()
            self.assertEqual(len(cloud.created), 1)
            self.assertEqual(cloud.certs[cloud.created[0]]["managed"]["domains"], RIYADH)
            self.assertEqual(cloud.attached, before + cloud.created)
            publish_certificates()
        self.assertEqual((len(cloud.created), cloud.updates), (1, 1))

    def test_publish_with_a_certificate_map_uses_no_classic_certificate(self) -> None:
        # Rule: with a certificate map on the proxy, publish uploads and routes,
        # and creates and attaches no classic certificate.
        cloud = with_certificate_map(production_before_riyadh(PublishCloud))
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(deploy, "gcloud", cloud):
            deploy.publish(built_output(Path(directory) / "out"), Path(directory) / "state")
        commands = [c[:3] for c in cloud.calls]
        self.assertIn(("compute", "url-maps", "describe"), commands)
        self.assertNotIn(("compute", "url-maps", "import"), commands)
        self.assertEqual(
            cloud.calls[:3],
            [
                ("compute", "target-https-proxies", "describe", deploy.PROXY, "--global"),
                ("certificate-manager", "maps", "entries", "list", "--map=control", "--location=global"),
                ("certificate-manager", "certificates", "list", "--location=global"),
            ],
        )
        self.assertNotIn(("compute", "ssl-certificates", "create"), commands)
        self.assertNotIn(("compute", "target-https-proxies", "update"), commands)
        self.assertEqual((cloud.created, cloud.updates), ([], 0))

    def test_publish_with_a_certificate_map_changes_nothing_while_a_host_lacks_one(self) -> None:
        # Rule: publish changes nothing until every host has an ACTIVE
        # certificate through the map.
        cloud = with_certificate_map(
            production_before_riyadh(PublishCloud), states={"riyadhtokenexchange.com": "PROVISIONING"}
        )
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(deploy, "gcloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "map for riyadhtokenexchange.com, www.riyadhtokenexchange.com[.]"):
                deploy.publish(built_output(Path(directory) / "out"), Path(directory) / "state")
        self.assertEqual(
            [c[:3] for c in cloud.calls],
            [
                ("compute", "target-https-proxies", "describe"),
                ("certificate-manager", "maps", "entries"),
                ("certificate-manager", "certificates", "list"),
            ],
        )

    def test_certificate_map_gaps_follow_certificate_manager_selection(self) -> None:
        # An exact entry is used when present, even if the wildcard would serve;
        # otherwise the *.<parent> entry; a host with neither is a gap. The
        # selected entry and its certificate must both be ACTIVE, so a PENDING
        # exact entry is a gap even beside an ACTIVE wildcard.
        cloud = FakeCloud({}, [])
        cloud.map_entries = {
            "a.com": ["ok"],
            "*.a.com": ["ok"],
            "www.b.com": ["pending"],
            "*.b.com": ["ok"],
            "d.com": ["ok"],
            "*.d.com": ["ok"],
            "www.e.com": ["ok"],
            "*.e.com": ["ok"],
        }
        cloud.map_states = {"ok": "ACTIVE", "pending": "PROVISIONING"}
        cloud.map_entry_states = {"d.com": "PENDING", "www.e.com": "PENDING"}
        with mock.patch.object(deploy, "gcloud", cloud):
            gaps = deploy.certificate_map_gaps(
                "maps/control",
                ["a.com", "www.a.com", "www.b.com", "c.b.com", "c.com", "d.com", "www.d.com", "www.e.com"],
            )
        self.assertEqual(gaps, ["www.b.com", "c.com", "d.com", "www.e.com"])

    def test_publish_certificates_attaches_an_existing_matching_certificate(self) -> None:
        cloud = production_before_riyadh()
        [(name, _)] = certificate_requests([], RIYADH)
        cloud.certs[name] = {"name": name, "managed": {"domains": list(reversed(RIYADH))}}
        with mock.patch.object(deploy, "gcloud", cloud):
            publish_certificates()
        self.assertEqual(cloud.created, [])
        self.assertEqual(cloud.attached[-1], name)

    def test_publish_certificates_refuses_other_domains_under_a_requested_name(self) -> None:
        cloud = production_before_riyadh()
        [(name, _)] = certificate_requests([], RIYADH)
        cloud.certs[name] = {"name": name, "managed": {"domains": ["wrong.example.com"]}}
        with mock.patch.object(deploy, "gcloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "exists for other domains"):
                publish_certificates()
        self.assertEqual((cloud.created, cloud.updates), ([], 0))

    def test_publish_certificates_refuses_when_the_proxy_changes_during_creation(self) -> None:
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

    def test_publish_certificates_refuses_when_the_proxy_changes_while_nothing_is_needed(self) -> None:
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

    def test_publish_certificates_refuses_then_uses_a_certificate_attached_mid_run(self) -> None:
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

    def test_publish_certificates_refuses_when_a_requested_certificate_changes(self) -> None:
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

    def test_publish_certificates_refuses_when_an_attached_certificate_is_replaced(self) -> None:
        cloud = production_before_riyadh()
        shrunk: dict[str, Any] = {"name": "token-exchange-20260919-4", "managed": {"domains": ["x.example.com"]}}
        cloud.hooks[("compute", "ssl-certificates", "create")] = lambda c: c.certs.update(
            {shrunk["name"]: shrunk}
        )
        with mock.patch.object(deploy, "gcloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "Attached certificates changed"):
                publish_certificates()
        self.assertEqual(cloud.updates, 0)

    def test_publish_certificates_refuses_past_the_proxy_limit(self) -> None:
        cloud = production_before_riyadh()
        cloud.attached += ["extra-1", "extra-2"]
        with mock.patch.object(deploy, "gcloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "Certificate limit reached"):
                publish_certificates()
        self.assertEqual((cloud.created, cloud.updates), ([], 0))

    def test_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            build(output)
            self.assertEqual(len(list(output.glob("*/index.html"))), 13)
            for market in load_markets():
                sitemap = ET.parse(output / market["slug"] / "sitemap.xml")  # noqa: S314 — trusted build output
                namespace = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
                paths = ["/"]
                if market["slug"] == "new-york":
                    paths += ["/confidential", "/suppliers", "/providers", "/badge", "/media-kit", "/index", "/trusted-cowork", "/buy", "/neocloud-accelerator"]
                self.assertEqual(
                    [node.text for node in sitemap.findall("s:url/s:loc", namespace)],
                    [f'https://{market["domain"]}{path}' for path in paths],
                )
                self.assertIn(
                    f'Sitemap: https://{market["domain"]}/sitemap.xml',
                    (output / market["slug"] / "robots.txt").read_text(),
                )
            # Every copied asset is referenced by the built pages, styles or scripts, and nothing
            # referenced is missing.
            assets = output / "assets"
            referenced = set()
            for path in [*output.glob("*/index.html"), *output.glob("*.html"), output / "index", *assets.glob("*.css"), *assets.glob("*.js")]:
                referenced.update(re.findall(r"/assets/([A-Za-z0-9._-]+)", path.read_text()))
            self.assertLessEqual({"exchange.css", "archivo.woff2", "river-close.webp", "river-hero.webp", "mark.svg"}, referenced)
            self.assertEqual({path.name for path in assets.iterdir()}, referenced)


if __name__ == "__main__":
    unittest.main()
