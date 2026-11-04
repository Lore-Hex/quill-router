from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from trusted_router import exchange_analytics as collector
from trusted_router.routes import public as public_routes

ORIGIN = "https://nytokenexchange.com"
HEADERS = {"Origin": ORIGIN, "Sec-Fetch-Site": "cross-site", "User-Agent": "Mozilla/5.0"}


@pytest.fixture(autouse=True)
def reset_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collector, "_WINDOW", -1)


def test_every_owned_origin_accepts_preflight_and_events(client: TestClient) -> None:
    for origin in public_routes._EXCHANGE_ORIGINS:
        headers = {**HEADERS, "Origin": origin}
        response = client.options("/token-exchange/events", headers=headers)
        assert response.status_code == 204
        assert response.headers["Access-Control-Allow-Origin"] == origin
        assert "Access-Control-Allow-Credentials" not in response.headers
        response = client.post("/token-exchange/events", headers=headers, json={
            "event": "exchange.landing_viewed", "visitor_id": str(uuid4()),
        })
        assert response.status_code == 204


def test_visits_and_downloads_share_one_visitor_fingerprint_across_exchange_sites(
    client: TestClient, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr(public_routes, "get_email_service", lambda _: SimpleNamespace(send=lambda _: True))
    public_routes._INQUIRY_HITS.clear()
    public_routes._INQUIRY_GLOBAL_HITS.clear()
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    visitor = str(uuid4())
    campaign = {"utm_source": "trustedrouter.com", "utm_content": "homepage-banner"}
    for name in ("exchange.landing_viewed", "exchange.landing_engaged"):
        assert client.post("/token-exchange/events", headers=HEADERS, json={
            "event": name, "visitor_id": visitor, "campaign": campaign,
        }).status_code == 204
    assert client.post("/token-exchange/brief", headers=HEADERS, json={
        "email": "reader@example.com", "visitor_id": visitor, "campaign": campaign,
    }).status_code == 200
    events = [r for r in caplog.records if getattr(r, "site_domain", "") == "nytokenexchange.com"]
    assert len(events) == 3
    assert len({r.__dict__["anonymous_fingerprint"] for r in events}) == 1
    assert events[0].__dict__["anonymous_fingerprint"] != visitor
    assert all(r.__dict__["creative_id"] == "homepage-banner" for r in events)
    assert all(not hasattr(r, "email") and not hasattr(r, "visitor_id") for r in events)
    from scripts.axiom_growth.model import project_event
    from trusted_router.main import _ApplicationConsoleFormatter

    serialized = json.loads(_ApplicationConsoleFormatter().format(events[0]))
    exported = project_event({**serialized, "_time": "2026-10-14T17:00:00Z"}, source="cloud_logging")
    assert exported["site_domain"] == "nytokenexchange.com"
    assert exported["page_path"] == "/"
    assert exported["creative_id"] == "homepage-banner"
    assert "reader@example.com" not in json.dumps(exported)
    # The same browser on a sister exchange site (the ID rides on the market-rail link) is the same visitor.
    other = next(o for o in public_routes._EXCHANGE_ORIGINS if o != ORIGIN)
    client.post("/token-exchange/events", headers={**HEADERS, "Origin": other}, json={
        "event": "exchange.landing_viewed", "visitor_id": visitor,
    })
    assert caplog.records[-1].__dict__["anonymous_fingerprint"] == events[0].__dict__["anonymous_fingerprint"]
    assert caplog.records[-1].__dict__["site_domain"] != "nytokenexchange.com"


@pytest.mark.parametrize("signal", [{"DNT": "1"}, {"Sec-GPC": "1"}])
def test_privacy_signals_suppress_exchange_logs(client: TestClient, caplog: pytest.LogCaptureFixture, signal: dict[str, str]) -> None:
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    response = client.post("/token-exchange/events", headers={**HEADERS, **signal}, json={
        "event": "exchange.landing_viewed", "visitor_id": str(uuid4()),
    })
    assert response.status_code == 204
    assert not [r for r in caplog.records if getattr(r, "event", "").startswith("acquisition.exchange.")]


@pytest.mark.parametrize("extra", [
    {"event": "credit_purchase_completed"}, {"visitor_id": "reader@example.com"},
    {"email": "reader@example.com"}, {"page": "/private?secret=value"},
])
def test_unknown_metadata_rejected_without_echo(client: TestClient, extra: dict[str, str]) -> None:
    response = client.post("/token-exchange/events", headers=HEADERS, json={
        "event": "exchange.landing_viewed", "visitor_id": str(uuid4()), **extra,
    })
    assert response.status_code == 422
    assert not response.content


def test_collector_bounds_origins_bodies_and_rate(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    for origin in ("https://evil.example", "https://nytokenexchange.com.evil.example", "null"):
        assert client.options("/token-exchange/events", headers={"Origin": origin}).status_code == 403
        assert client.post("/token-exchange/events", headers={"Origin": origin}, json={}).status_code == 403
    assert client.post("/token-exchange/events", headers=HEADERS, json={"x": "x" * 2048}).status_code == 413
    assert client.post("/token-exchange/events", headers=HEADERS, content="x").status_code == 415
    monkeypatch.setattr(public_routes, "accept_event", lambda _: False)
    response = client.post("/token-exchange/events", headers=HEADERS, json={
        "event": "exchange.landing_viewed", "visitor_id": str(uuid4()),
    })
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"


def test_collector_memory_and_rate_caps_reset() -> None:
    assert all(collector.accept_event("client", now=120) for _ in range(120))
    assert not collector.accept_event("client", now=120)
    for i in range(6500):
        collector.accept_event(str(i), now=120)
    assert len(collector._CLIENTS) <= 4096
    assert not collector.accept_event("new", now=120)
    assert collector.accept_event("client", now=180)


@pytest.mark.parametrize(('name', 'properties'), [
    ('nyte.cta_clicked', {'cta': 'api_quick_start', 'position': 'hero'}),
    ('nyte.video_played', {}),
    *[('nyte.cta_clicked', {'cta': cta, 'position': position})
      for cta in ('download_apple_silicon', 'download_intel_mac')
      for position in ('hero', 'close')],
    ('nyte.reserve_checkout_started', {'unit': 'blocks', 'underlying_kind': 'index', 'grade': 'A'}),
    ('nyte.reserve_checkout_succeeded', {}),
    ('nyte.resale_listed', {'grade': 'A'}),
    ('nyte.resale_withdrawn', {'grade': 'B'}),
    ('nyte.buy_step_1_viewed', {'unit': 'blocks', 'underlying_kind': 'index', 'grade': 'A'}),
    ('nyte.buy_step_2_viewed', {'unit': 'blocks', 'underlying_kind': 'index', 'grade': 'A'}),
    ('nyte.buy_step_3_viewed', {'unit': 'blocks', 'underlying_kind': 'index', 'grade': 'A'}),
    ('nyte.buy_step_4_viewed', {'unit': 'blocks', 'underlying_kind': 'index', 'grade': 'A'}),
    ('nyte.buy_completed', {'unit': 'blocks', 'underlying_kind': 'index', 'grade': 'A'}),
    ('nyte.buy_completed', {'unit': 'throughput', 'underlying_kind': 'model', 'grade': 'none'}),
    *[(name, {'unit': 'blocks', 'underlying_kind': 'facility', 'grade': 'none'})
      for name in ['nyte.buy_step_1_viewed', 'nyte.buy_step_2_viewed', 'nyte.buy_step_3_viewed',
                   'nyte.buy_step_4_viewed', 'nyte.buy_completed']],
    ('nyte.form_submitted', {'form': 'buyer_deck'}),
    ('nyte.outbound_clicked', {'destination_host': 'trustedrouter.com', 'cta': 'api_key'}),
])
def test_nyte_events_survive_logging_and_export(
    client: TestClient, caplog: pytest.LogCaptureFixture, name: str, properties: dict[str, str],
) -> None:
    from scripts.axiom_growth.model import project_event
    from scripts.axiom_growth.provision import source_filter
    from trusted_router.main import _ApplicationConsoleFormatter

    caplog.set_level(logging.INFO, logger='trusted_router.acquisition')
    response = client.post('/token-exchange/events', headers=HEADERS, json={
        'event': name, 'visitor_id': str(uuid4()), 'page': '/manage-reservation' if name.startswith('nyte.resale_') else '/buy' if name.startswith(('nyte.buy_', 'nyte.reserve_')) else '/confidential', **properties,
    })
    assert response.status_code == 204
    record = next(r for r in caplog.records if getattr(r, 'event', '') == 'acquisition.' + name)
    serialized = json.loads(_ApplicationConsoleFormatter().format(record))
    exported = project_event({**serialized, '_time': '2026-10-21T17:00:00Z'}, source='cloud_logging')
    assert f'jsonPayload.event="acquisition.{name}"' in source_filter()
    assert exported['site_domain'] == 'nytokenexchange.com'
    assert exported['page_path'] == ('/manage-reservation' if name.startswith('nyte.resale_') else '/buy' if name.startswith(('nyte.buy_', 'nyte.reserve_')) else '/confidential')
    for key, value in properties.items():
        assert exported[key] == value


@pytest.mark.parametrize('properties', [
    {'cta': 'reader@example.com', 'position': 'hero'},
    {'cta': 'api_quick_start'},
    {'cta': 'api_quick_start', 'position': 'hero', 'form': 'buyer_deck'},
    {'cta': 'api_quick_start', 'position': 'hero', 'email': 'reader@example.com'},
])
def test_nyte_event_properties_are_bounded(client: TestClient, properties: dict[str, str]) -> None:
    response = client.post('/token-exchange/events', headers=HEADERS, json={
        'event': 'nyte.cta_clicked', 'visitor_id': str(uuid4()), **properties,
    })
    assert response.status_code == 422
    assert not response.content


@pytest.mark.parametrize('extra', [
    {'underlying_kind': 'facility'}, {'underlying_kind': 'solar'},
    {'underlying_kind': 'facility', 'grade': 'none', 'unit': 'throughput'},
    {'underlying_kind': 'facility', 'grade': 'none', 'unit': 'unsure'},
    {'facility': 'solar'}, {'facility_name': 'Underwater Datacenter'},
    {'unit': 'someone@example.com'}, {'underlying_kind': 'free text'},
    {'grade': 'private model'}, {'name': 'Private Name'}, {'email': 'person@example.com'},
    {'company': 'Private Company'}, {'model': 'private model'}, {'grade': 'none'},
])
def test_capacity_metadata_rejects_private_or_inconsistent_fields(client: TestClient, extra: dict[str, str]) -> None:
    response = client.post('/token-exchange/events', headers=HEADERS, json={
        'event': 'nyte.buy_completed', 'visitor_id': str(uuid4()), 'page': '/buy',
        'unit': 'blocks', 'underlying_kind': 'index', 'grade': 'A', **extra,
    })
    assert response.status_code == 422


_BROWSER_PATHS = [
    route + suffix for route in ("/neocloud-accelerator", "/trusted-cowork", "/buy")
    for suffix in ("", ".html")
]


@pytest.fixture(scope="module")
def browser_payloads() -> dict[str, list[dict]]:
    # One bounded Node startup per worker, fresh isolated VM for every page.
    # The six pages each retain separate 1s script and callback VM deadlines.
    # Allow process startup on a loaded runner outside those execution limits.
    # Preserve stderr to distinguish startup stalls from page-code failures.
    harness = Path(__file__).parent / "js" / "exchange_analytics_harness.cjs"
    try:
        result = subprocess.run(  # noqa: S603
            ["node", str(harness), *_BROWSER_PATHS],  # noqa: S607
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"analytics harness exceeded 60s; progress: {exc.stderr!r}")
    payloads = json.loads(result.stdout)
    assert set(payloads) == set(_BROWSER_PATHS), result.stderr
    return payloads


@pytest.mark.xdist_group("exchange-browser-payloads")
@pytest.mark.parametrize("pathname", _BROWSER_PATHS)
def test_nyte_browser_payloads_survive_collector_logging_and_export(
    client: TestClient, caplog: pytest.LogCaptureFixture, pathname: str,
    browser_payloads: dict[str, list[dict]],
) -> None:
    from scripts.axiom_growth.model import project_event
    from scripts.axiom_growth.provision import source_filter
    from trusted_router.main import _ApplicationConsoleFormatter

    canonical = pathname.removesuffix(".html")
    payloads = browser_payloads[pathname]
    assert [payload["event"] for payload in payloads] == [
        "exchange.landing_viewed", "exchange.landing_engaged",
    ]
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    for payload in payloads:
        assert payload["page"] == canonical
        assert collector.ExchangeEvent.model_validate(payload).page == canonical
        response = client.post("/token-exchange/events", headers=HEADERS, json=payload)
        assert response.status_code == 204
        event = "acquisition." + payload["event"]
        record = next(r for r in caplog.records if getattr(r, "event", "") == event)
        assert record.__dict__["page_path"] == canonical
        serialized = json.loads(_ApplicationConsoleFormatter().format(record))
        assert serialized["page_path"] == canonical
        assert f'jsonPayload.event="{event}"' in source_filter()
        exported = project_event(
            {**serialized, "_time": "2026-10-26T17:00:00Z"}, source="cloud_logging",
        )
        assert exported["event"] == event
        assert exported["site_domain"] == "nytokenexchange.com"
        assert exported["page_path"] == canonical


def _exported(caplog: pytest.LogCaptureFixture, event_name: str) -> tuple[logging.LogRecord, dict[str, object]]:
    from scripts.axiom_growth.model import project_event
    from trusted_router.main import _ApplicationConsoleFormatter

    record = next(r for r in caplog.records if getattr(r, "event", "") == event_name)
    serialized = json.loads(_ApplicationConsoleFormatter().format(record))
    return record, project_event({**serialized, "_time": "2026-11-02T17:00:00Z"}, source="cloud_logging")


def test_referrer_is_the_source_when_the_page_has_no_tags(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    assert client.post("/token-exchange/events", headers=HEADERS, json={
        "event": "exchange.landing_viewed", "visitor_id": str(uuid4()), "referrer_host": "news.ycombinator.com",
    }).status_code == 204
    record, exported = _exported(caplog, "acquisition.exchange.landing_viewed")
    assert (record.__dict__["utm_source"], record.__dict__["utm_medium"]) == ("news.ycombinator.com", "referral")
    assert exported["utm_source"] == "hackernews"
    assert exported["referrer_domain"] == "news.ycombinator.com"


def test_untagged_visits_are_direct_and_never_name_the_site_as_their_source(
    client: TestClient, caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    for payload in ({}, {"referrer_host": "nytokenexchange.com"}, {"campaign": {"utm_source": "nytokenexchange.com"}}):
        caplog.clear()
        assert client.post("/token-exchange/events", headers=HEADERS, json={
            "event": "exchange.landing_viewed", "visitor_id": str(uuid4()), **payload,
        }).status_code == 204
        record, exported = _exported(caplog, "acquisition.exchange.landing_viewed")
        assert record.__dict__["utm_source"] == "direct"
        assert exported["utm_source"] == "direct"
        assert (exported["site_domain"], exported["market"]) == ("nytokenexchange.com", "new-york")


def test_new_pages_and_buttons_count_under_their_own_names(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    visitor, page = str(uuid4()), "/a-page-joseph-adds-next-week"
    assert client.post("/token-exchange/events", headers=HEADERS, json={
        "event": "exchange.landing_viewed", "visitor_id": visitor, "page": page,
    }).status_code == 204
    assert client.post("/token-exchange/events", headers=HEADERS, json={
        "event": "nyte.cta_clicked", "visitor_id": visitor, "page": page, "cta": "book_pilot_call", "position": "hero",
    }).status_code == 204
    assert client.post("/token-exchange/events", headers=HEADERS, json={
        "event": "nyte.outbound_clicked", "visitor_id": visitor, "page": "/buy", "cta": "book_pilot_call",
        "destination_host": "calendly.com",
    }).status_code == 204
    assert _exported(caplog, "acquisition.exchange.landing_viewed")[1]["page_path"] == page
    assert _exported(caplog, "acquisition.nyte.cta_clicked")[1]["cta"] == "book_pilot_call"
    assert _exported(caplog, "acquisition.nyte.outbound_clicked")[1]["destination_host"] == "calendly.com"
    for bad in ({"page": "/Has Spaces"}, {"page": "/two/segments"}, {"page": "/" + "a" * 41}, {"page": "/buy.html"}):
        assert client.post("/token-exchange/events", headers=HEADERS, json={
            "event": "exchange.landing_viewed", "visitor_id": visitor, **bad,
        }).status_code == 422
    assert client.post("/token-exchange/events", headers=HEADERS, json={
        "event": "nyte.outbound_clicked", "visitor_id": visitor, "cta": "source", "destination_host": "evil.example",
    }).status_code == 422
    assert client.post("/token-exchange/events", headers=HEADERS, json={
        "event": "exchange.landing_viewed", "visitor_id": visitor, "referrer_host": "https://news.ycombinator.com/",
    }).status_code == 422


def test_exchange_events_carry_market_device_and_one_visitor_hash(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="trusted_router.acquisition")
    visitor = str(uuid4())
    phone = {**HEADERS, "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148 Safari/604.1"}
    assert client.post("/token-exchange/events", headers=phone, json={
        "event": "exchange.landing_viewed", "visitor_id": visitor,
    }).status_code == 204
    assert client.post("/token-exchange/events", headers={**HEADERS, "Origin": "https://thetokenexchange.com"}, json={
        "event": "exchange.landing_engaged", "visitor_id": visitor,
    }).status_code == 204
    _, new_york = _exported(caplog, "acquisition.exchange.landing_viewed")
    _, world = _exported(caplog, "acquisition.exchange.landing_engaged")
    assert (new_york["market"], new_york["exchange_market"], new_york["device_category"]) == ("new-york", "new-york", "mobile")
    assert (world["site_domain"], world["market"]) == ("thetokenexchange.com", "global")
    assert new_york["anonymous_fingerprint"] == world["anonymous_fingerprint"] == new_york["exchange_visitor_fingerprint"]
    assert visitor not in json.dumps([new_york, world])


def test_paid_reservation_exports_with_its_site_market_and_amount() -> None:
    import re

    from scripts.axiom_growth.model import project_event
    from scripts.axiom_growth.provision import source_filter
    from trusted_router.services.reserved_tokens import purchase_attribution

    visitor_hash = "b" * 64
    fields = purchase_attribution(
        {"visitor_fingerprint": visitor_hash, "site_domain": "nytokenexchange.com", "market": "new-york"}, 123_456,
    )
    row = project_event({
        "_time": "2026-11-02T17:00:00Z", "event": "acquisition.reserved_tokens_recorded",
        "stripe_session_id": "cs_private", "stripe_customer_id": "cus_private", **fields,
    }, source="cloud_logging")
    assert 'jsonPayload.event="acquisition.reserved_tokens_recorded"' in source_filter()
    assert row["anonymous_fingerprint"] == row["exchange_visitor_fingerprint"] == visitor_hash
    assert (row["site_domain"], row["market"], row["page_path"], row["amount_microdollars"]) == (
        "nytokenexchange.com", "new-york", "/buy", 1_234_560_000)
    assert not {"stripe_session_id", "stripe_customer_id"} & row.keys()
    anonymous = purchase_attribution({}, 5000)
    assert re.fullmatch(r"[a-f0-9]{64}", anonymous["anonymous_fingerprint"])
    assert anonymous["exchange_visitor_fingerprint"] == ""
