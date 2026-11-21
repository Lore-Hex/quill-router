from __future__ import annotations

import dataclasses
import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.clickhouse_activity import ActivityClickHouse
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.console import activity as activity_routes
from trusted_router.storage import STORE
from trusted_router.storage_models import Generation
from trusted_router.storage_operational_analytics import analytics_surrogate


def _signed_in_client(
    *,
    email: str = "client-reliability@example.com",
) -> tuple[TestClient, str]:
    app = create_app(Settings(environment="test"), init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user(email)
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label=email,
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    return client, workspace.id


class _FakeAnalytics:
    def __init__(self) -> None:
        self.summary_tenants: list[str] = []
        self.event_tenants: list[str] = []
        self.activity_tenants: list[str] = []

    def client_reliability_summary(
        self,
        tenant_id: str,
        *,
        window_minutes: int,
    ) -> dict[str, Any]:
        self.summary_tenants.append(tenant_id)
        assert window_minutes in {60, 360, 1440, 10080}
        return {
            "requests": 200,
            "successes": 198,
            "tr_fault": 2,
            "excluded": 3,
            "aborted": 1,
            "attempts": 220,
            "failover_used": 10,
            "first_attempt_success": 180,
            "p50_total_ms": 200,
            "p95_total_ms": 800,
            "p50_ttft_ms": 100,
            "by_host": {},
        }

    def client_events_recent(
        self,
        tenant_id: str,
        *,
        since: dt.datetime,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        self.event_tenants.append(tenant_id)
        assert since.tzinfo is not None
        assert limit == 50
        return [
            {
                "created_at": "2026-08-17T10:45:00Z",
                "endpoint": "responses",
                "model": "openai/gpt-5",
                "attempt_host": ["apex", "ally"],
                "attempt_count": 2,
                "final_outcome": "transport_error",
                "final_http_status": None,
                "first_error_class": "connect_timeout",
                "sdk": "tr-py",
                "sdk_version": "0.6.0",
                "attempt_request_id": ["rlog_0123456789abcdef0123456789abcdef"],
            }
        ]

    def activity_elapsed_summary(
        self,
        *,
        tenant_id: str,
        start_at: str,
    ) -> dict[str, int | None]:
        self.activity_tenants.append(tenant_id)
        assert start_at.endswith("Z")
        return {"generations": 3, "samples": 3, "median_ms": 20}


class _WholeRangeAnalytics(_FakeAnalytics):
    """The fake's client telemetry, with the real elapsed-time SQL on fixture rows."""

    def __init__(self, database: ActivityClickHouse) -> None:
        super().__init__()
        self._client = database.client()

    def activity_elapsed_summary(
        self,
        *,
        tenant_id: str,
        start_at: str,
    ) -> dict[str, int | None]:
        self.activity_tenants.append(tenant_id)
        return self._client.activity_elapsed_summary(tenant_id=tenant_id, start_at=start_at)


def test_client_reliability_unavailable_is_a_200() -> None:
    activity_routes._CLIENT_RELIABILITY_CACHE.clear()
    client, _workspace_id = _signed_in_client()

    response = client.get("/console/activity/client-reliability.json?range=24h")

    assert response.status_code == 200
    assert response.json() == {"data": None, "meta": {"reason": "unavailable"}}


def test_client_reliability_rejects_bad_range() -> None:
    activity_routes._CLIENT_RELIABILITY_CACHE.clear()
    client, _workspace_id = _signed_in_client()

    response = client.get("/console/activity/client-reliability.json?range=30d")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "invalid range"


def test_client_reliability_envelope_and_workspace_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity_routes._CLIENT_RELIABILITY_CACHE.clear()
    client, workspace_id = _signed_in_client()
    user = STORE.find_user_by_email("client-reliability@example.com")
    assert user is not None
    other = STORE.create_workspace(user.id, "Other workspace", trial_credit_microdollars=0)
    fake = _FakeAnalytics()
    monkeypatch.setattr(activity_routes, "_operational_analytics_client", lambda _settings: fake)

    response = client.get("/console/activity/client-reliability.json?range=1h")

    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"] == {
        "scanned": 4,
        "truncated": False,
        "freshness_seconds": 0,
    }
    assert payload["data"]["summary"]["availability_percent"] == 99.0
    assert payload["data"]["summary"]["retried_percent"] == 10.0
    assert payload["data"]["summary"]["failover_percent"] == 5.0
    assert payload["data"]["server_p50_elapsed_ms"] == 20
    assert payload["data"]["top_errors"] == [
        {"error_class": "connect_timeout", "http_status": None, "count": 1}
    ]
    expected = analytics_surrogate("workspace", workspace_id)
    other_surrogate = analytics_surrogate("workspace", other.id)
    assert fake.summary_tenants == [expected]
    assert fake.event_tenants == [expected]
    assert fake.activity_tenants == [expected]
    assert other_surrogate not in {
        *fake.summary_tenants,
        *fake.event_tenants,
        *fake.activity_tenants,
    }


def test_client_reliability_server_median_covers_the_whole_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A busy hour: the newest 5,000 rows are fast, the 5,002 before them slow.

    The median of the range is the slow value. A median over the newest 5,000
    rows only, which the page used to show, is the fast one.
    """
    activity_routes._CLIENT_RELIABILITY_CACHE.clear()
    client, workspace_id = _signed_in_client(email="client-median@example.com")
    database = ActivityClickHouse()
    now = dt.datetime.now(dt.UTC)
    template = Generation(
        id="template",
        request_id="req-median",
        workspace_id=workspace_id,
        key_hash="key-median",
        model="test/model",
        provider_name="Test Provider",
        app="test",
        tokens_prompt=1,
        tokens_completion=1,
        total_cost_microdollars=1,
        usage_type="Credits",
        speed_tokens_per_second=1.0,
        finish_reason="stop",
        status="success",
        streamed=False,
    )
    for index in range(10_002):
        created = now - dt.timedelta(minutes=1, milliseconds=index * 300)
        database.add(
            dataclasses.replace(
                template,
                id=f"gen-median-{index}",
                elapsed_milliseconds=100 if index < 5_000 else 900,
                created_at=created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            )
        )
    # Outside the hour, and another workspace's: neither moves the median.
    database.add(
        dataclasses.replace(
            template,
            id="gen-median-old",
            elapsed_milliseconds=5,
            created_at=(now - dt.timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
        )
    )
    database.add(
        dataclasses.replace(
            template,
            id="gen-median-other",
            workspace_id="ws-someone-else",
            elapsed_milliseconds=5,
            created_at=(now - dt.timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        )
    )
    fake = _WholeRangeAnalytics(database)
    monkeypatch.setattr(activity_routes, "_operational_analytics_client", lambda _settings: fake)

    response = client.get("/console/activity/client-reliability.json?range=1h")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["server_p50_elapsed_ms"] == 900
    # Every generation of the range and the one sampled failure: nothing cut off.
    assert payload["meta"] == {
        "scanned": 10_003,
        "truncated": False,
        "freshness_seconds": 0,
    }
    assert fake.activity_tenants == [analytics_surrogate("workspace", workspace_id)]
    [query] = database.queries
    assert "FROM activity_generations FINAL" in query
    assert "LIMIT" not in query


def test_client_reliability_has_no_server_median_without_timed_generations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity_routes._CLIENT_RELIABILITY_CACHE.clear()
    client, _workspace_id = _signed_in_client(email="client-no-median@example.com")
    fake = _WholeRangeAnalytics(ActivityClickHouse())
    monkeypatch.setattr(activity_routes, "_operational_analytics_client", lambda _settings: fake)

    response = client.get("/console/activity/client-reliability.json?range=24h")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data"]["server_p50_elapsed_ms"] is None
    assert payload["meta"]["scanned"] == 1
    assert payload["meta"]["truncated"] is False


def test_client_reliability_cache_expires_after_sixty_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    activity_routes._CLIENT_RELIABILITY_CACHE.clear()
    client, _workspace_id = _signed_in_client(email="client-cache@example.com")
    fake = _FakeAnalytics()
    monkeypatch.setattr(activity_routes, "_operational_analytics_client", lambda _settings: fake)
    timestamps = iter((0.0, 61.0))
    monkeypatch.setattr(
        activity_routes,
        "time",
        SimpleNamespace(monotonic=lambda: next(timestamps)),
    )

    first = client.get("/console/activity/client-reliability.json?range=6h")
    second = client.get("/console/activity/client-reliability.json?range=6h")

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(fake.summary_tenants) == 2


def test_activity_template_contains_client_panel_and_empty_state() -> None:
    client, _workspace_id = _signed_in_client(email="client-panel@example.com")

    response = client.get("/console/activity")

    assert response.status_code == 200
    assert "data-client-panel" in response.text
    assert "/console/activity/client-reliability.json" in response.text
    assert "from your SDKs · content-free" in response.text
    assert "No client telemetry yet." in response.text
    assert "TrustedRouter SDKs ≥ py 0.6.0" in response.text
    assert (
        response.text.index("<h2>Usage</h2>")
        < response.text.index("<h2>Client-observed reliability</h2>")
        < response.text.index("<h2>Recent activity</h2>")
    )
