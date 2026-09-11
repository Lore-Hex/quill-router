from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from trusted_router.synthetic import route_health


def test_expiring_catalog_alerts_are_provider_grouped_and_metadata_only(monkeypatch):
    now = datetime(2026, 9, 11, tzinfo=UTC)
    expired = now - timedelta(hours=1)
    endpoints = {
        "a": SimpleNamespace(provider="near-ai", catalog_valid_until=expired),
        "b": SimpleNamespace(provider="near-ai", catalog_valid_until=expired),
        "c": SimpleNamespace(provider="healthy", catalog_valid_until=now + timedelta(days=10)),
        "d": SimpleNamespace(provider="static", catalog_valid_until=None),
        "e": SimpleNamespace(provider="soon", catalog_valid_until=now + timedelta(hours=24)),
    }
    events = []
    monkeypatch.setattr("trusted_router.synthetic.alerts.ops_alert", lambda message, **kw: events.append((message, kw)))
    assert route_health.report_catalog_freshness(endpoints=endpoints.values(), now=now) == ["near-ai", "soon"]
    assert len(events) == 2
    assert events[0][1]["fingerprint"] == ["catalog-freshness", "near-ai"]
    assert "expired" in events[0][0]
    assert "expires" in events[1][0]


def test_sustained_availability_alert_does_not_include_provider_text(monkeypatch):
    events = []
    monkeypatch.setattr("trusted_router.synthetic.alerts.ops_alert", lambda message, **kw: events.append((message, kw)))
    flag = route_health.RouteHealthFlag("near-ai", "m", 6, 6, 1.0, "error", "PRIVATE", "availability")
    route_health.report_route_health([flag])
    assert len(events) == 1
    assert "PRIVATE" not in repr(events)
    assert events[0][1]["fingerprint"] == ["route-availability", "near-ai", "m"]


def test_availability_alerts_never_quarantine_routes(monkeypatch):
    from trusted_router.config import Settings
    from trusted_router.synthetic.remediator import _detect_route_quarantine

    flags = [
        route_health.RouteHealthFlag("near-ai", "m", 6, 6, 1.0, "timeout", None, "availability"),
        route_health.RouteHealthFlag("broken", "n", 6, 6, 1.0, "not_found", None),
    ]
    monkeypatch.setattr(route_health, "evaluate_route_health", lambda _store: flags)
    decisions = _detect_route_quarantine(Settings(environment="test"))
    assert [decision.subject for decision in decisions] == ["broken/n"]
