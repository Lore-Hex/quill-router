from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.console.activity import _USAGE_CACHE, _UsageCache
from trusted_router.storage import STORE, Generation, InMemoryStore
from trusted_router.storage_activity import usage_bucket_key


def _generation(
    generation_id: str,
    *,
    workspace_id: str = "ws_1",
    key_hash: str = "key_a",
    model: str = "model-a",
    usage_type: str = "Credits",
    created_at: str,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int,
    cost_micro: int,
) -> Generation:
    return Generation(
        id=generation_id,
        request_id=f"req-{generation_id}",
        workspace_id=workspace_id,
        key_hash=key_hash,
        model=model,
        provider_name="Provider",
        app="usage-series-test",
        tokens_prompt=prompt_tokens,
        tokens_completion=completion_tokens,
        reasoning_tokens=reasoning_tokens,
        total_cost_microdollars=cost_micro,
        usage_type=usage_type,
        speed_tokens_per_second=10.0,
        finish_reason="stop",
        status="success",
        streamed=False,
        created_at=created_at,
    )


def test_usage_bucket_key_all_granularities() -> None:
    assert usage_bucket_key("2026-05-01T14:23:45Z", "minute") == "2026-05-01T14:23"
    assert usage_bucket_key("2026-05-01T14:23:45Z", "5min") == "2026-05-01T14:20"
    assert usage_bucket_key("2026-05-01T14:23:45Z", "hour") == "2026-05-01T14"
    assert usage_bucket_key("2026-05-01T14:23:45Z", "day") == "2026-05-01"
    assert usage_bucket_key("2026-05-01T14:00:00Z", "5min") == "2026-05-01T14:00"
    assert usage_bucket_key("2026-05-01T14:04:00Z", "5min") == "2026-05-01T14:00"
    assert usage_bucket_key("2026-05-01T14:05:00Z", "5min") == "2026-05-01T14:05"
    assert usage_bucket_key("2026-05-01T14:59:00Z", "5min") == "2026-05-01T14:55"
    with pytest.raises(ValueError, match="unknown granularity"):
        usage_bucket_key("2026-05-01T14:23:45Z", "week")


def test_memory_usage_series_hourly_uses_rolling_24h_window() -> None:
    store = InMemoryStore()
    user = store.ensure_user("rolling-usage@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    _raw_key, api_key = store.create_api_key(
        workspace_id=workspace.id,
        name="rolling usage key",
        creator_user_id=user.id,
    )
    now = dt.datetime.now(dt.UTC)
    rows = [
        ("gen-2h", now - dt.timedelta(hours=2), 100),
        ("gen-10h", now - dt.timedelta(hours=10), 200),
        ("gen-30h", now - dt.timedelta(hours=30), 300),
    ]
    for generation_id, created_at, cost_micro in rows:
        store.add_generation(
            _generation(
                generation_id,
                workspace_id=workspace.id,
                key_hash=api_key.hash,
                created_at=created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                prompt_tokens=10,
                completion_tokens=5,
                reasoning_tokens=0,
                cost_micro=cost_micro,
            )
        )

    hourly = store.usage_series(workspace.id, window_minutes=1440, granularity="hour")
    daily = store.usage_series(workspace.id, window_minutes=43200, granularity="day")

    assert sum(int(bucket["requests"]) for bucket in hourly["buckets"]) == 2
    assert sum(int(bucket["cost_micro"]) for bucket in hourly["buckets"]) == 300
    assert all("T" in str(bucket["bucket"]) for bucket in hourly["buckets"])
    assert all(len(str(bucket["bucket"])) == 13 for bucket in hourly["buckets"])
    assert sum(int(bucket["requests"]) for bucket in daily["buckets"]) == 3
    assert sum(int(bucket["cost_micro"]) for bucket in daily["buckets"]) == 600


def test_memory_usage_series_minute_uses_rolling_60_minute_window() -> None:
    store = InMemoryStore()
    user = store.ensure_user("minute-usage@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    _raw_key, api_key = store.create_api_key(
        workspace_id=workspace.id,
        name="minute usage key",
        creator_user_id=user.id,
    )
    now = dt.datetime.now(dt.UTC).replace(microsecond=0)
    included_at = now - dt.timedelta(minutes=15)
    excluded_at = now - dt.timedelta(minutes=75)
    for generation_id, created_at, cost_micro in [
        ("gen-15m", included_at, 100),
        ("gen-75m", excluded_at, 200),
    ]:
        store.add_generation(
            _generation(
                generation_id,
                workspace_id=workspace.id,
                key_hash=api_key.hash,
                created_at=created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
                prompt_tokens=10,
                completion_tokens=5,
                reasoning_tokens=0,
                cost_micro=cost_micro,
            )
        )

    minute = store.usage_series(workspace.id, window_minutes=60, granularity="minute")

    assert minute["buckets"] == [
        {
            "bucket": included_at.strftime("%Y-%m-%dT%H:%M"),
            "requests": 1,
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "reasoning_tokens": 0,
            "cost_micro": 100,
            "byok_micro": 0,
        }
    ]


def test_console_usage_series_endpoint_returns_json_and_uses_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _USAGE_CACHE.clear()
    app = create_app(Settings(environment="local"), init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("usage-route@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="usage-route@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)
    calls: list[tuple[str, int, str, str | None, bool]] = []

    def spy_usage_series(
        self: InMemoryStore,
        workspace_id: str,
        *,
        window_minutes: int,
        granularity: str,
        api_key_hash: str | None = None,
        by_model: bool = False,
    ) -> dict[str, Any]:
        _ = self
        calls.append((workspace_id, window_minutes, granularity, api_key_hash, by_model))
        return {
            "granularity": granularity,
            "start_day": "2026-07-06",
            "end_day": "2026-07-07",
            "truncated": False,
            "buckets": [],
            "by_model": {"model-a": []} if by_model else {},
        }

    monkeypatch.setattr(InMemoryStore, "usage_series", spy_usage_series)

    first = client.get("/console/activity/usage.json?range=1h&by_model=true&api_key_hash=key_a")
    second = client.get("/console/activity/usage.json?range=1h&by_model=true&api_key_hash=key_a")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["granularity"] == "minute"
    assert first.json()["range"] == "1h"
    assert first.json()["latest_activity_at"] is None
    assert first.json() == second.json()
    assert calls == [(workspace.id, 60, "minute", "key_a", True)]


def test_console_usage_cache_is_bounded_and_evicts_lru() -> None:
    cache = _UsageCache(max_entries=2)
    first = ("ws-1", "1h", False, None)
    second = ("ws-2", "1h", False, None)
    third = ("ws-3", "1h", False, None)

    cache.put(first, {"value": 1}, expires_at=100.0)
    cache.put(second, {"value": 2}, expires_at=100.0)
    assert cache.get(first, now=1.0) == {"value": 1}

    cache.put(third, {"value": 3}, expires_at=100.0)

    assert len(cache) == 2
    assert cache.get(first, now=1.0) == {"value": 1}
    assert cache.get(second, now=1.0) is None
    assert cache.get(third, now=1.0) == {"value": 3}


def test_console_usage_cache_removes_expired_entries() -> None:
    cache = _UsageCache(max_entries=2)
    key = ("ws-1", "30d", True, "key-hash")
    cache.put(key, {"value": 1}, expires_at=10.0)

    assert cache.get(key, now=10.0) is None
    assert len(cache) == 0


def test_console_usage_series_empty_window_reports_latest_activity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _USAGE_CACHE.clear()
    app = create_app(Settings(environment="local"), init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("usage-latest@example.com")
    workspace = STORE.list_workspaces_for_user(user.id)[0]
    raw_token, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="usage-latest@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)

    def empty_usage_series(
        self: InMemoryStore,
        workspace_id: str,
        *,
        window_minutes: int,
        granularity: str,
        api_key_hash: str | None = None,
        by_model: bool = False,
    ) -> dict[str, Any]:
        _ = (self, workspace_id, window_minutes, api_key_hash, by_model)
        return {
            "granularity": granularity,
            "start_day": "2026-07-15",
            "end_day": "2026-07-15",
            "truncated": False,
            "buckets": [],
        }

    def latest_activity(
        self: InMemoryStore,
        workspace_id: str,
        *,
        api_key_hash: str | None = None,
        date: str | None = None,
        limit: int = 100,
        tag_key: str | None = None,
        tag_value: str | None = None,
    ) -> list[dict[str, Any]]:
        _ = (self, date, tag_key, tag_value)
        assert workspace_id == workspace.id
        assert api_key_hash == "key-a"
        assert limit == 1
        return [{"created_at": "2026-07-14T15:34:16Z"}]

    monkeypatch.setattr(InMemoryStore, "usage_series", empty_usage_series)
    monkeypatch.setattr(InMemoryStore, "activity_events", latest_activity)

    response = client.get(
        "/console/activity/usage.json?range=1h&api_key_hash=key-a"
    )

    assert response.status_code == 200
    assert response.json()["buckets"] == []
    assert response.json()["latest_activity_at"] == "2026-07-14T15:34:16Z"


def test_console_usage_series_endpoint_rejects_bad_range() -> None:
    _USAGE_CACHE.clear()
    app = create_app(Settings(environment="local"), init_observability=False)
    client = TestClient(app)
    user = STORE.ensure_user("usage-bad-granularity@example.com")
    raw_token, _session = STORE.create_auth_session(
        user_id=user.id,
        provider="google",
        label="usage-bad-granularity@example.com",
        ttl_seconds=3600,
        state="active",
    )
    client.cookies.set("tr_session", raw_token)

    response = client.get("/console/activity/usage.json?range=bad")

    assert response.status_code == 400
    assert response.json()["error"]["message"] == "invalid range"
