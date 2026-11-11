"""User-facing routes degrade, not 500, when ClickHouse is unreachable.

Regression for QUILL-ROUTER-G1 (/status.json) and QUILL-ROUTER-G3
(/console/activity): every ClickHouse endpoint's connect timed out,
``send_with_failover`` re-raised ``httpx.ConnectTimeout``, and nothing between
it and the route caught it.

The failing reads go through a real ``OperationalAnalyticsClient`` whose
transport raises ``httpx.ConnectTimeout``, so the exception reaching each route
is exactly the one production raised. Patches target the backend CLASS, never
the ``STORE`` proxy (issue #333).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.operational_analytics import (
    OperationalAnalyticsClient,
    is_analytics_unavailable,
)
from trusted_router.routes.console import activity as activity_routes
from trusted_router.storage import STORE, InMemoryStore

ANALYTICS_LOGGER_EVENT = "operational_analytics.unavailable"


def _unreachable_clickhouse() -> OperationalAnalyticsClient:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    return OperationalAnalyticsClient(
        base_url="http://clickhouse-a.internal:8123,http://clickhouse-b.internal:8123",
        user="reader",
        password="inert",  # noqa: S106 - inert test credential.
        transport=httpx.MockTransport(handler),
    )


def _read_through_unreachable_clickhouse(*_args: Any, **_kwargs: Any) -> Any:
    return _unreachable_clickhouse()._query("SELECT 1 FORMAT JSON")


def _app_client() -> TestClient:
    return TestClient(create_app(Settings(environment="test"), init_observability=False))


def _signed_in_client() -> TestClient:
    client = _app_client()
    user = STORE.ensure_user("clickhouse-outage@example.com")
    raw_token, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label=user.email,
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client


@pytest.fixture(autouse=True)
def _clear_usage_cache() -> Any:
    activity_routes._USAGE_CACHE.clear()
    yield
    activity_routes._USAGE_CACHE.clear()


def test_the_simulated_failure_is_the_production_exception() -> None:
    with pytest.raises(httpx.ConnectTimeout) as raised:
        _read_through_unreachable_clickhouse()
    assert is_analytics_unavailable(raised.value)


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (httpx.ConnectTimeout("t"), True),
        (httpx.ConnectError("refused"), True),
        (httpx.ReadTimeout("slow"), True),
        (httpx.RemoteProtocolError("disconnected"), True),
        (503, True),
        (502, True),
        (504, True),
        (500, False),
        (400, False),
        (RuntimeError("ClickHouse response did not contain data rows"), False),
        (ValueError("bad"), False),
    ],
)
def test_only_unreachability_counts_as_unavailable(exc: Any, expected: bool) -> None:
    if isinstance(exc, int):
        request = httpx.Request("POST", "http://clickhouse.internal:8123")
        exc = httpx.HTTPStatusError(
            "status", request=request, response=httpx.Response(exc, request=request)
        )
    assert is_analytics_unavailable(exc) is expected


# ------------------------------------------------------------------ /status.json


def test_status_json_is_200_and_unknown_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(
        InMemoryStore, "synthetic_probe_samples", _read_through_unreachable_clickhouse
    )
    monkeypatch.setattr(InMemoryStore, "synthetic_rollups", _read_through_unreachable_clickhouse)

    with caplog.at_level(logging.WARNING):
        response = _app_client().get("/status.json")

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    # Unknown is the truth; nothing may read as healthy.
    assert data["overall_status"] == "unknown"
    assert data["overall_status_label"] == "Unknown"
    assert data["summary"]["headline"] == "Status Data Temporarily Unavailable"
    assert "not a report that systems are healthy" in data["summary"]["detail"]
    assert data["samples"] == []
    assert data["monitor_freshness"]["is_stale"] is True
    assert all(component["status"] != "up" for component in data["components"])
    # The schema is intact, including the drain-lag section the fleet
    # checker requires on every deployment.
    for key in ("components", "windows", "slo_classes", "current", "analytics"):
        assert key in data
    records = [r for r in caplog.records if ANALYTICS_LOGGER_EVENT in r.getMessage()]
    assert records, "the ClickHouse outage must still be logged"
    assert all(r.levelno == logging.WARNING for r in records)
    assert getattr(records[0], "error_class", None) == "ConnectTimeout"


def test_status_page_renders_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        InMemoryStore, "synthetic_probe_samples", _read_through_unreachable_clickhouse
    )
    monkeypatch.setattr(InMemoryStore, "synthetic_rollups", _read_through_unreachable_clickhouse)

    response = _app_client().get("/status", headers={"accept": "text/html"})

    assert response.status_code == 200, response.text
    assert "Status Data Temporarily Unavailable" in response.text
    assert "All Systems Operational" not in response.text


def test_status_history_is_a_retryable_503_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        InMemoryStore, "synthetic_probe_samples", _read_through_unreachable_clickhouse
    )

    response = _app_client().get("/status/history?window=24h&format=json")

    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == "30"


def test_status_json_still_raises_on_a_non_availability_defect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A schema or code defect is not an outage and must not be masked."""

    def broken(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("ClickHouse response did not contain data rows")

    monkeypatch.setattr(InMemoryStore, "synthetic_probe_samples", broken)

    client = TestClient(
        create_app(Settings(environment="test"), init_observability=False),
        raise_server_exceptions=False,
    )
    assert client.get("/status.json").status_code == 500


# ------------------------------------------------------------- /console/activity


def test_console_activity_renders_unavailable_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _signed_in_client()
    monkeypatch.setattr(InMemoryStore, "activity_events", _read_through_unreachable_clickhouse)

    with caplog.at_level(logging.WARNING):
        response = client.get("/console/activity")

    assert response.status_code == 200, response.text
    assert "data-activity-unavailable" in response.text
    assert "Activity is temporarily unavailable" in response.text
    # Not the empty-account message: that would claim there was no activity.
    assert "No activity yet. Make a request" not in response.text.split("Recent activity")[1]
    records = [r for r in caplog.records if ANALYTICS_LOGGER_EVENT in r.getMessage()]
    assert records and all(r.levelno == logging.WARNING for r in records)


def test_console_activity_still_shows_the_empty_state_when_reachable() -> None:
    response = _signed_in_client().get("/console/activity")

    assert response.status_code == 200, response.text
    assert "data-activity-unavailable" not in response.text


def test_console_usage_json_is_a_retryable_503_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _signed_in_client()
    monkeypatch.setattr(InMemoryStore, "usage_series", _read_through_unreachable_clickhouse)

    response = client.get("/console/activity/usage.json?range=24h")

    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == "30"
    # A failure is never cached as if it were a result.
    assert len(activity_routes._USAGE_CACHE) == 0


# ------------------------------------------------------------------ /v1/activity


@pytest.mark.parametrize("group_by", [None, "request"])
def test_api_activity_is_a_retryable_503_when_clickhouse_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, group_by: str | None
) -> None:
    client = _signed_in_client()
    monkeypatch.setattr(
        InMemoryStore, "activity_events_result", _read_through_unreachable_clickhouse
    )
    monkeypatch.setattr(InMemoryStore, "activity_result", _read_through_unreachable_clickhouse)

    params = {"group_by": group_by} if group_by else {}
    response = client.get("/v1/activity", params=params)

    assert response.status_code == 503, response.text
    assert response.headers.get("retry-after") == "30"
    assert response.json()["error"]["type"] == "service_unavailable"
