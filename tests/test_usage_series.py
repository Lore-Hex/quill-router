from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.spanner import make_fake_store
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.routes.console.activity import _USAGE_CACHE
from trusted_router.storage import STORE, Generation, InMemoryStore
from trusted_router.storage_activity import summarize_activity, usage_bucket_key
from trusted_router.storage_gcp_activity_index import usage_series, write_generation


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


def _seed_generations() -> tuple[Any, list[Generation]]:
    _store, _db, table = make_fake_store()
    generations = [
        _generation(
            "gen-1",
            created_at="2026-05-01T10:05:00Z",
            prompt_tokens=10,
            completion_tokens=5,
            reasoning_tokens=1,
            cost_micro=100,
        ),
        _generation(
            "gen-2",
            key_hash="key_b",
            model="model-b",
            usage_type="BYOK",
            created_at="2026-05-01T10:45:00Z",
            prompt_tokens=20,
            completion_tokens=10,
            reasoning_tokens=2,
            cost_micro=200,
        ),
        _generation(
            "gen-3",
            created_at="2026-05-01T11:00:00Z",
            prompt_tokens=30,
            completion_tokens=15,
            reasoning_tokens=3,
            cost_micro=300,
        ),
        _generation(
            "gen-4",
            model="model-b",
            created_at="2026-05-02T09:15:00Z",
            prompt_tokens=40,
            completion_tokens=20,
            reasoning_tokens=4,
            cost_micro=400,
        ),
        _generation(
            "gen-other-workspace",
            workspace_id="ws_other",
            created_at="2026-05-01T10:10:00Z",
            prompt_tokens=99,
            completion_tokens=99,
            reasoning_tokens=99,
            cost_micro=99,
        ),
    ]
    for generation in generations:
        write_generation(table, "m", generation)
    return table, generations[:4]


def test_usage_series_hourly_and_daily_buckets() -> None:
    table, _generations = _seed_generations()

    hourly = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="hour",
    )
    daily = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="day",
    )

    assert hourly["buckets"] == [
        {
            "bucket": "2026-05-01T10",
            "requests": 2,
            "prompt_tokens": 30,
            "completion_tokens": 15,
            "reasoning_tokens": 3,
            "cost_micro": 100,
            "byok_micro": 200,
        },
        {
            "bucket": "2026-05-01T11",
            "requests": 1,
            "prompt_tokens": 30,
            "completion_tokens": 15,
            "reasoning_tokens": 3,
            "cost_micro": 300,
            "byok_micro": 0,
        },
        {
            "bucket": "2026-05-02T09",
            "requests": 1,
            "prompt_tokens": 40,
            "completion_tokens": 20,
            "reasoning_tokens": 4,
            "cost_micro": 400,
            "byok_micro": 0,
        },
    ]
    assert daily["buckets"] == [
        {
            "bucket": "2026-05-01",
            "requests": 3,
            "prompt_tokens": 60,
            "completion_tokens": 30,
            "reasoning_tokens": 6,
            "cost_micro": 400,
            "byok_micro": 200,
        },
        {
            "bucket": "2026-05-02",
            "requests": 1,
            "prompt_tokens": 40,
            "completion_tokens": 20,
            "reasoning_tokens": 4,
            "cost_micro": 400,
            "byok_micro": 0,
        },
    ]
    assert table.reads[0] == (
        b"ws#ws_1#2026-05-01",
        b"ws#ws_1#2026-05-02~",
        200_000,
    )


def test_usage_series_minute_and_5min_buckets() -> None:
    _store, _db, table = make_fake_store()
    write_generation(
        table,
        "m",
        _generation(
            "gen-5min",
            created_at="2026-05-01T14:23:00Z",
            prompt_tokens=10,
            completion_tokens=5,
            reasoning_tokens=1,
            cost_micro=100,
        ),
    )

    minute = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-01",
        granularity="minute",
    )
    five_min = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-01",
        granularity="5min",
    )

    assert minute["buckets"][0]["bucket"] == "2026-05-01T14:23"
    assert five_min["buckets"][0]["bucket"] == "2026-05-01T14:20"


@pytest.mark.parametrize(
    ("granularity", "first_bucket"),
    [
        ("hour", "2026-05-01T10"),
        ("minute", "2026-05-01T10:45"),
        ("5min", "2026-05-01T10:45"),
    ],
)
def test_usage_series_rolling_cutoff_uses_start_key(
    granularity: str,
    first_bucket: str,
) -> None:
    table, _generations = _seed_generations()

    series = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity=granularity,
        min_created_at="2026-05-01T10:30:00",
    )

    assert table.reads[-1][0] == b"ws#ws_1#2026-05-01#2026-05-01T10:30:00"
    assert table.reads[-1][1] == b"ws#ws_1#2026-05-02~"
    assert sum(int(bucket["requests"]) for bucket in series["buckets"]) == 3
    assert series["buckets"][0]["bucket"] == first_bucket
    assert series["buckets"][0]["requests"] == 1


def test_usage_series_by_model_breakdown() -> None:
    table, _generations = _seed_generations()

    series = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="day",
        by_model=True,
    )

    assert series["by_model"] == {
        "model-a": [
            {
                "bucket": "2026-05-01",
                "requests": 2,
                "prompt_tokens": 40,
                "completion_tokens": 20,
                "reasoning_tokens": 4,
                "cost_micro": 400,
                "byok_micro": 0,
            }
        ],
        "model-b": [
            {
                "bucket": "2026-05-01",
                "requests": 1,
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "reasoning_tokens": 2,
                "cost_micro": 0,
                "byok_micro": 200,
            },
            {
                "bucket": "2026-05-02",
                "requests": 1,
                "prompt_tokens": 40,
                "completion_tokens": 20,
                "reasoning_tokens": 4,
                "cost_micro": 400,
                "byok_micro": 0,
            },
        ],
    }


def test_usage_series_day_totals_match_summarize_activity() -> None:
    table, generations = _seed_generations()

    series = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="day",
    )

    expected: dict[str, dict[str, int]] = {}
    for row in summarize_activity(generations):
        bucket = expected.setdefault(
            str(row["date"]),
            {
                "requests": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "reasoning_tokens": 0,
                "cost_micro": 0,
                "byok_micro": 0,
            },
        )
        bucket["requests"] += int(row["requests"])
        bucket["prompt_tokens"] += int(row["prompt_tokens"])
        bucket["completion_tokens"] += int(row["completion_tokens"])
        bucket["reasoning_tokens"] += int(row["reasoning_tokens"])
        bucket["cost_micro"] += int(row["usage_microdollars"])
        bucket["byok_micro"] += int(row["byok_usage_inference_microdollars"])
    actual = {
        str(bucket["bucket"]): {
            key: int(bucket[key])
            for key in (
                "requests",
                "prompt_tokens",
                "completion_tokens",
                "reasoning_tokens",
                "cost_micro",
                "byok_micro",
            )
        }
        for bucket in series["buckets"]
    }

    assert actual == expected


def test_usage_series_api_key_filter_and_truncation() -> None:
    table, _generations = _seed_generations()

    filtered = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="hour",
        api_key_hash="key_b",
    )
    truncated = usage_series(
        table,
        "m",
        "ws_1",
        start_day="2026-05-01",
        end_day="2026-05-02",
        granularity="day",
        max_rows=2,
    )

    assert filtered["buckets"] == [
        {
            "bucket": "2026-05-01T10",
            "requests": 1,
            "prompt_tokens": 20,
            "completion_tokens": 10,
            "reasoning_tokens": 2,
            "cost_micro": 0,
            "byok_micro": 200,
        }
    ]
    assert truncated["truncated"] is True
    assert truncated["buckets"] == [
        {
            "bucket": "2026-05-01",
            "requests": 2,
            "prompt_tokens": 30,
            "completion_tokens": 15,
            "reasoning_tokens": 3,
            "cost_micro": 100,
            "byok_micro": 200,
        }
    ]
    assert table.reads[-1] == (
        b"ws#ws_1#2026-05-01",
        b"ws#ws_1#2026-05-02~",
        2,
    )


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
