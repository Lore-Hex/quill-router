from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from trusted_router.synthetic import route_health

DIGEST_HOUR = datetime(2026, 11, 18, route_health.DEGRADATION_DIGEST_HOUR_UTC, 1, tzinfo=UTC)


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


def test_a_row_deadline_alert_names_only_the_expiring_models(monkeypatch):
    # One model's promotion ends: the provider's other routes keep serving, so
    # the alert names those models instead of claiming the provider expires.
    # Same fingerprint, so it resolves the provider's existing issue.
    now = datetime(2026, 11, 11, 13, tzinfo=UTC)
    cutover = datetime(2026, 11, 12, tzinfo=UTC)
    later = now + timedelta(days=14)

    def route(model, deadline, kind="prepaid"):
        return SimpleNamespace(
            provider="decisionmodelsio", model_id=f"decisionmodelsio/{model}",
            id=f"decisionmodelsio/{model}@decisionmodelsio/{kind}", catalog_valid_until=deadline,
        )

    endpoints = [
        route("s1-pro", cutover), route("s1-pro", cutover, "byok"), route("s1-pro-2", cutover),
        route("s1-fast", later), route("s1-pro-3", later),
    ]
    events = []
    monkeypatch.setattr("trusted_router.synthetic.alerts.ops_alert", lambda message, **kw: events.append((message, kw)))
    assert route_health.report_catalog_freshness(endpoints=endpoints, now=now) == ["decisionmodelsio"]
    (message, kw), = events
    assert kw["fingerprint"] == ["catalog-freshness", "decisionmodelsio"]
    assert message.startswith(
        "catalog-freshness: decisionmodelsio models decisionmodelsio/s1-pro, "
        "decisionmodelsio/s1-pro-2 expire within 48 hours; deadline=2026-11-12T00:00:00+00:00."
    )
    assert "Only these models' routes are affected" in message
    assert "s1-fast" not in message and "s1-pro-3" not in message
    events.clear()
    assert route_health.report_catalog_freshness(endpoints=endpoints, now=cutover) == ["decisionmodelsio"]
    assert " expired; " in events[0][0] and "s1-pro-2" in events[0][0]
    # Once the provider's own deadline is near too, the alert is provider-wide.
    events.clear()
    route_health.report_catalog_freshness(endpoints=endpoints, now=later - timedelta(hours=1))
    assert events[0][0].startswith("catalog-freshness: decisionmodelsio expired;")
    assert "Repair discovery" in events[0][0]


@pytest.mark.parametrize("kind", ["availability", "degradation"])
def test_sustained_availability_alert_does_not_include_provider_text(monkeypatch, kind):
    events = []
    monkeypatch.setattr("trusted_router.synthetic.alerts.ops_alert", lambda message, **kw: events.append((message, kw)))
    flag = route_health.RouteHealthFlag("near-ai", "m", 6, 6, 1.0, "error", "PRIVATE", kind)
    route_health.report_route_health([flag], now=DIGEST_HOUR)
    assert len(events) == 1
    assert "PRIVATE" not in repr(events)
    assert events[0][1]["fingerprint"] == (
        ["route-availability", "near-ai", "m"] if kind == "availability"
        else ["route-degradation-digest", "2026-11-18"]
    )


@pytest.mark.parametrize(
    ("error_type", "error_status", "suffix"),
    [
        ("insufficient_quota", 402, " (latest: insufficient_quota, HTTP 402)"),
        ("TimeoutError", None, " (latest: TimeoutError)"),
        (None, 503, " (latest: HTTP 503)"),
        # Negative controls: provider-shaped text and impossible statuses
        # never reach the alert, and nothing usable means no suffix at all.
        ("PRIVATE provider text sk-123", 502, " (latest: HTTP 502)"),
        ("x" * 65, None, ""),
        ("", 0, ""),
        (None, 9999, ""),
        (None, None, ""),
    ],
)
@pytest.mark.parametrize("kind", ["availability", "degradation"])
def test_availability_alert_carries_only_error_class_and_status(
    monkeypatch, kind, error_type, error_status, suffix,
):
    events = []
    monkeypatch.setattr("trusted_router.synthetic.alerts.ops_alert", lambda message, **kw: events.append((message, kw)))
    flag = route_health.RouteHealthFlag(
        "chutes", "m", 24, 6, 0.25, error_type, "PRIVATE", kind, newest_error_status=error_status,
    )
    route_health.report_route_health([flag], now=DIGEST_HOUR)
    (message, kw), = events
    if kind == "degradation":
        assert message.endswith("chutes/m 6/24 (25%)" + suffix)
        assert kw["fingerprint"] == ["route-degradation-digest", "2026-11-18"]
    else:
        assert message.endswith("minutes" + suffix)
        assert kw["fingerprint"] == ["route-availability", "chutes", "m"]
    assert "PRIVATE" not in message and "sk-" not in message


def _flag(provider, kind="degradation", **kw):
    return route_health.RouteHealthFlag(
        provider, "glm-5.3-flash", 28, 7, 0.25, "provider_error", "PRIVATE sk-1", kind,
        newest_error_status=502, **kw,
    )


def test_flaky_route_only_logs_outside_the_daily_digest_hour(monkeypatch, caplog):
    events = []
    monkeypatch.setattr("trusted_router.synthetic.alerts.ops_alert", lambda message, **kw: events.append((message, kw)))
    off_hour = DIGEST_HOUR.replace(hour=(route_health.DEGRADATION_DIGEST_HOUR_UTC + 1) % 24)
    with caplog.at_level("WARNING", logger="trusted_router.synthetic.route_health"):
        route_health.report_route_health([_flag("near-ai")], now=off_hour)
    assert events == []
    assert "route-degradation: near-ai/glm-5.3-flash failed 7/28" in caplog.text
    assert "PRIVATE" not in caplog.text and "sk-" not in caplog.text


def test_daily_digest_groups_every_flaky_route_into_one_alert_and_outages_still_page(monkeypatch):
    events = []
    monkeypatch.setattr("trusted_router.synthetic.alerts.ops_alert", lambda message, **kw: events.append((message, kw)))
    flags = [_flag("near-ai"), _flag("fortanix"), _flag("zro", kind="availability")]
    route_health.report_route_health(flags, now=DIGEST_HOUR)
    digest = [e for e in events if e[1]["fingerprint"][0] == "route-degradation-digest"]
    pages = [e for e in events if e[1]["fingerprint"][0] == "route-availability"]
    (message, kw), = digest
    assert message.startswith("route-degradation daily digest 2026-11-18: 2 flaky routes")
    assert message.index("fortanix/") < message.index("near-ai/")
    assert kw["tags"] == {"degraded_routes": "2"}
    assert [e[1]["fingerprint"] for e in pages] == [["route-availability", "zro", "glm-5.3-flash"]]
    # The next day's digest is a new issue, not a reopened one.
    events.clear()
    route_health.report_route_health(flags[:1], now=DIGEST_HOUR + timedelta(days=1))
    assert events[0][1]["fingerprint"] == ["route-degradation-digest", "2026-11-19"]
    assert events[0][0].startswith("route-degradation daily digest 2026-11-19: 1 flaky route failed")


def test_availability_alerts_never_quarantine_routes(monkeypatch):
    from trusted_router.config import Settings
    from trusted_router.synthetic.remediator import _detect_route_quarantine

    flags = [
        route_health.RouteHealthFlag("near-ai", "m", 6, 6, 1.0, "timeout", None, "availability"),
        route_health.RouteHealthFlag("degraded", "m", 24, 6, 0.25, "timeout", None, "degradation"),
        route_health.RouteHealthFlag("broken", "n", 6, 6, 1.0, "not_found", None),
    ]
    monkeypatch.setattr(route_health, "evaluate_route_health", lambda _store: flags)
    decisions = _detect_route_quarantine(Settings(environment="test"))
    assert [decision.subject for decision in decisions] == ["broken/n"]
