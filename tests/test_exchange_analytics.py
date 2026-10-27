from __future__ import annotations

import json
import logging
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


def test_visits_and_downloads_share_only_a_site_scoped_fingerprint(
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
    other = next(o for o in public_routes._EXCHANGE_ORIGINS if o != ORIGIN)
    client.post("/token-exchange/events", headers={**HEADERS, "Origin": other}, json={
        "event": "exchange.landing_viewed", "visitor_id": visitor,
    })
    assert caplog.records[-1].__dict__["anonymous_fingerprint"] != events[0].__dict__["anonymous_fingerprint"]


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
    from trusted_router.main import _ApplicationConsoleFormatter

    caplog.set_level(logging.INFO, logger='trusted_router.acquisition')
    response = client.post('/token-exchange/events', headers=HEADERS, json={
        'event': name, 'visitor_id': str(uuid4()), 'page': '/buy' if name.startswith('nyte.buy_') else '/confidential', **properties,
    })
    assert response.status_code == 204
    record = next(r for r in caplog.records if getattr(r, 'event', '') == 'acquisition.' + name)
    serialized = json.loads(_ApplicationConsoleFormatter().format(record))
    exported = project_event({**serialized, '_time': '2026-10-21T17:00:00Z'}, source='cloud_logging')
    assert exported['site_domain'] == 'nytokenexchange.com'
    assert exported['page_path'] == ('/buy' if name.startswith('nyte.buy_') else '/confidential')
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
    {'underlying_kind': 'facility'}, {'underlying_kind': 'greenference'},
    {'underlying_kind': 'facility', 'grade': 'none', 'unit': 'throughput'},
    {'underlying_kind': 'facility', 'grade': 'none', 'unit': 'unsure'},
    {'facility': 'greenference'}, {'facility_name': 'Submerge'},
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
