from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.fakes.clickhouse_activity import ActivityClickHouse
from trusted_router.config import Settings
from trusted_router.main import create_app
from trusted_router.operational_analytics import usage_buckets_query
from trusted_router.routes.console.activity import (
    _USAGE_CACHE,
    USAGE_RANGE_PRESETS,
    _UsageCache,
)
from trusted_router.storage import STORE, Generation, InMemoryStore
from trusted_router.storage_activity import usage_bucket_key
from trusted_router.storage_operational_analytics_reads import OperationalAnalyticsReads


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


def test_console_usage_series_skips_the_latest_lookup_when_the_range_has_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the empty state shows the most recent request, so only it reads one."""
    _USAGE_CACHE.clear()
    client, _workspace_id = _signed_in_client("usage-no-latest@example.com")
    bucket = {
        "bucket": "2026-07-15",
        "requests": 2,
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "reasoning_tokens": 0,
        "cost_micro": 300,
        "byok_micro": 0,
    }

    def usage_series(self: InMemoryStore, workspace_id: str, **kwargs: Any) -> dict[str, Any]:
        _ = (self, workspace_id)
        return {
            "granularity": kwargs["granularity"],
            "start_day": "2026-07-15",
            "end_day": "2026-07-15",
            "truncated": False,
            "buckets": [bucket],
        }

    def no_lookup(self: InMemoryStore, workspace_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("a range with usage must not look up the latest request")

    monkeypatch.setattr(InMemoryStore, "usage_series", usage_series)
    monkeypatch.setattr(InMemoryStore, "activity_events", no_lookup)

    response = client.get("/console/activity/usage.json?range=7d")

    assert response.status_code == 200
    assert response.json()["buckets"] == [bucket]
    assert response.json()["latest_activity_at"] is None


# ---------------------------------------------------------------------------
# The ClickHouse path. The durable stores read the usage series as a grouped
# aggregate; the in-memory store buckets generations in Python. The fixture
# below goes to both, and they must agree bucket for bucket.

_PARITY_NOW = dt.datetime(2026, 11, 1, 0, 7, 30, tzinfo=dt.UTC)
# Each timestamp sits on an edge of one of the console ranges at _PARITY_NOW,
# on a UTC day or month boundary, or on a bucket boundary.
_PARITY_ROWS: list[tuple[str, str, str, str]] = [
    # (created_at, key, model, usage type)
    ("2026-08-03T23:59:59.999Z", "a", "model-a", "Credits"),  # last ms before 90d
    ("2026-08-04T00:00:00.000Z", "a", "model-a", "Credits"),  # first ms of 90d
    ("2026-09-30T23:59:59.999Z", "b", "model-b", "BYOK"),  # month boundary
    ("2026-10-01T00:00:00.000Z", "a", "model-b", "Credits"),
    ("2026-10-02T23:59:59.999Z", "a", "model-a", "Credits"),  # last ms before 30d
    ("2026-10-03T00:00:00.000Z", "b", "model-a", "\tBYOK\t"),  # first ms of 30d, tabs stored
    ("2026-10-25T23:59:59.999Z", "a", "model-b", "Credits"),  # last ms before 7d
    ("2026-10-26T00:00:00.000Z", "a", "model-a", "Credits"),  # first ms of 7d
    ("2026-10-31T00:07:29.999Z", "b", "model-a", "Credits"),  # just outside 24h
    ("2026-10-31T00:07:30.000Z", "a", "model-b", "BYOK"),  # first ms of 24h
    ("2026-10-31T00:59:59.999Z", "a", "model-a", "Credits"),  # hour boundary
    ("2026-10-31T01:00:00.000Z", "b", "model-a", "Credits"),
    ("2026-10-31T18:07:29.999Z", "a", "model-a", "Credits"),  # just outside 6h
    ("2026-10-31T18:07:30.000Z", "a", "model-b", "Credits"),  # first ms of 6h
    ("2026-10-31T23:04:59.999Z", "a", "model-a", "Credits"),
    ("2026-10-31T23:07:29.999Z", "b", "model-a", "Credits"),  # just outside 1h
    ("2026-10-31T23:07:30.000Z", "a", "model-a", "Credits"),  # first ms of 1h
    ("2026-10-31T23:59:59.999Z", "a", "model-a", "Credits"),  # last ms of October
    ("2026-11-01T00:00:00.000Z", "b", "model-b", "BYOK"),  # first ms of November
    ("2026-11-01T00:04:59.999Z", "a", "model-a", "Credits"),  # 5-minute boundary
    ("2026-11-01T00:05:00.000Z", "a", "model-a", "Credits"),
    ("2026-11-01T00:05:00.001Z", "b", "model-b", "Credits"),  # same bucket, other model
    ("2026-11-01T00:05:59.999Z", "a", "model-a", " Byok "),  # a stored value, not canonical
    ("2026-11-01T00:30:00.000Z", "a", "model-a", "Credits"),  # later today
    ("2026-11-02T00:00:00.000Z", "a", "model-a", "Credits"),  # tomorrow: out of range
]


class _ClickHouseReads(OperationalAnalyticsReads):
    """The reads mixin the durable stores share, over the fake ClickHouse."""

    def __init__(self, database: ActivityClickHouse) -> None:
        self._operational_analytics = database.client()


def _freeze_clock(monkeypatch: pytest.MonkeyPatch, now: dt.datetime) -> None:
    class Clock(dt.datetime):
        @classmethod
        def now(cls, tz: dt.tzinfo | None = None) -> dt.datetime:  # type: ignore[override]
            return now.astimezone(tz)

    frozen = SimpleNamespace(**(vars(dt) | {"datetime": Clock}))
    # Only the two readers' clocks, not datetime everywhere.
    monkeypatch.setattr("trusted_router.storage_generations.dt", frozen)
    monkeypatch.setattr("trusted_router.storage_operational_analytics_reads.dt", frozen)


def _parity_fixture() -> tuple[InMemoryStore, ActivityClickHouse, str, dict[str, str]]:
    store = InMemoryStore()
    user = store.ensure_user("usage-parity@example.com")
    workspace = store.list_workspaces_for_user(user.id)[0]
    keys = {
        name: store.create_api_key(
            workspace_id=workspace.id, name=f"parity {name}", creator_user_id=user.id
        )[1].hash
        for name in ("a", "b")
    }
    database = ActivityClickHouse()
    for index, (created_at, key, model, usage_type) in enumerate(_PARITY_ROWS):
        generation = _generation(
            f"gen-parity-{index}",
            workspace_id=workspace.id,
            key_hash=keys[key],
            model=model,
            usage_type=usage_type,
            created_at=created_at,
            prompt_tokens=100 + index,
            completion_tokens=10 + index,
            reasoning_tokens=index % 3,
            cost_micro=1_000 + 7 * index,
        )
        store.add_generation(generation)
        # A Generation normalizes its usage type. The column gets the value
        # as written here, so the reader has " Byok " and tabs to normalize.
        stored = {"usage_type": usage_type}
        if index % 5 == 0:
            # A first delivery with other numbers, then the settled row: the
            # later delivery is the one that counts.
            database.add(generation, **stored | {"tokens_prompt": 1, "total_cost_microdollars": 1})
        database.add(generation, **stored)
        if index % 4 == 0:
            database.add(generation, **stored)  # an exact redelivery
    # Another workspace's request in the same minute as one of ours.
    database.add(
        _generation(
            "gen-parity-other-workspace",
            workspace_id="ws-someone-else",
            created_at="2026-11-01T00:05:00.000Z",
            prompt_tokens=9_999,
            completion_tokens=9_999,
            reasoning_tokens=9_999,
            cost_micro=9_999,
        )
    )
    return store, database, workspace.id, keys


@pytest.mark.parametrize("range_", sorted(USAGE_RANGE_PRESETS))
@pytest.mark.parametrize("by_model", [False, True])
@pytest.mark.parametrize("key", [None, "a", "b"])
def test_clickhouse_usage_series_matches_the_in_memory_store(
    monkeypatch: pytest.MonkeyPatch, range_: str, by_model: bool, key: str | None
) -> None:
    _freeze_clock(monkeypatch, _PARITY_NOW)
    store, database, workspace_id, keys = _parity_fixture()
    window_minutes, granularity = USAGE_RANGE_PRESETS[range_]
    kwargs: dict[str, Any] = {
        "window_minutes": window_minutes,
        "granularity": granularity,
        "api_key_hash": keys[key] if key else None,
        "by_model": by_model,
    }

    expected = store.usage_series(workspace_id, **kwargs)
    result = _ClickHouseReads(database).usage_series(workspace_id, **kwargs)

    assert result == expected
    assert result["truncated"] is False
    assert ("by_model" in result) is by_model
    # The fixture is not vacuous for this range and filter.
    assert sum(bucket["requests"] for bucket in result["buckets"]) > 0
    # One grouped query. Not the generations themselves, and not a capped read.
    [query] = database.queries
    assert query == usage_buckets_query(
        granularity=granularity, by_model=by_model, key_filtered=key is not None
    )
    assert "FROM activity_generations FINAL" in query
    assert "LIMIT" not in query


def test_clickhouse_usage_series_parity_fixture_has_teeth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fixture would catch a dropped FINAL, a merged usage type or a shifted day."""
    _freeze_clock(monkeypatch, _PARITY_NOW)
    store, database, workspace_id, _keys = _parity_fixture()

    expected = store.usage_series(workspace_id, window_minutes=129600, granularity="day")

    delivered = database.connection.execute("SELECT count(*) FROM activity_generations")
    # Replays are in the table: reading it without FINAL counts them again.
    assert delivered.fetchone()[0] > len(_PARITY_ROWS) + 1
    in_range = _PARITY_ROWS[1:-1]
    assert sum(bucket["requests"] for bucket in expected["buckets"]) == len(in_range)
    assert sum(bucket["byok_micro"] for bucket in expected["buckets"]) > 0
    assert sum(bucket["cost_micro"] for bucket in expected["buckets"]) > 0
    days = [bucket["bucket"] for bucket in expected["buckets"]]
    # Both sides of the September and October month boundaries, as UTC days.
    assert {"2026-09-30", "2026-10-01", "2026-10-31", "2026-11-01"} <= set(days)
    assert "2026-08-03" not in days and "2026-11-02" not in days


def _signed_in_client(email: str) -> tuple[TestClient, str]:
    app = create_app(Settings(environment="local"), init_observability=False)
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


def _route_through_clickhouse(
    monkeypatch: pytest.MonkeyPatch, database: ActivityClickHouse
) -> None:
    """Serve the console's store reads from the durable stores' reader."""
    reads = _ClickHouseReads(database)

    def usage_series(self: InMemoryStore, workspace_id: str, **kwargs: Any) -> dict[str, Any]:
        return reads.usage_series(workspace_id, **kwargs)

    def activity_events(
        self: InMemoryStore, workspace_id: str, **kwargs: Any
    ) -> list[dict[str, Any]]:
        return reads.activity_events(workspace_id, **kwargs)

    monkeypatch.setattr(InMemoryStore, "usage_series", usage_series)
    monkeypatch.setattr(InMemoryStore, "activity_events", activity_events)


def _recent(workspace_id: str, index: int, age: dt.timedelta, **fields: Any) -> Generation:
    created = dt.datetime.now(dt.UTC) - age
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "created_at": created.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "reasoning_tokens": 1,
        "cost_micro": 100,
    }
    values.update(fields)
    return _generation(f"gen-route-{index}", **values)


def test_console_usage_json_serves_the_grouped_series(monkeypatch: pytest.MonkeyPatch) -> None:
    _USAGE_CACHE.clear()
    client, workspace_id = _signed_in_client("usage-grouped@example.com")
    database = ActivityClickHouse()
    rows = [
        _recent(workspace_id, 0, dt.timedelta(days=2, hours=1)),
        _recent(workspace_id, 1, dt.timedelta(days=2), model="model-b", usage_type="BYOK"),
        _recent(workspace_id, 2, dt.timedelta(days=40)),
    ]
    for row in rows:
        database.add(row)
    database.add(rows[0])  # redelivered: still one request
    _route_through_clickhouse(monkeypatch, database)

    response = client.get("/console/activity/usage.json?range=30d&by_model=1")

    assert response.status_code == 200
    payload = response.json()
    assert payload["range"] == "30d"
    assert payload["granularity"] == "day"
    assert payload["truncated"] is False
    totals = {
        metric: sum(bucket[metric] for bucket in payload["buckets"])
        for metric in ("requests", "prompt_tokens", "completion_tokens", "cost_micro", "byok_micro")
    }
    assert totals == {
        "requests": 2,
        "prompt_tokens": 20,
        "completion_tokens": 10,
        "cost_micro": 100,
        "byok_micro": 100,
    }
    assert sorted(payload["by_model"]) == ["model-a", "model-b"]
    assert {bucket["bucket"] for bucket in payload["buckets"]} == {
        row.created_at[:10] for row in rows[:2]
    }
    # The range has usage: one grouped query and no lookup of the latest request.
    assert payload["latest_activity_at"] is None
    [query] = database.queries
    assert "GROUP BY bucket, model" in query


def test_console_usage_json_empty_range_finds_the_latest_request_in_a_recent_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _USAGE_CACHE.clear()
    client, workspace_id = _signed_in_client("usage-empty-range@example.com")
    database = ActivityClickHouse()
    newest = _recent(workspace_id, 0, dt.timedelta(hours=3))
    for row in (
        newest,
        _recent(workspace_id, 1, dt.timedelta(hours=4)),
        _recent(workspace_id, 2, dt.timedelta(days=300)),
    ):
        database.add(row)
    _route_through_clickhouse(monkeypatch, database)

    response = client.get("/console/activity/usage.json?range=1h")

    assert response.status_code == 200
    payload = response.json()
    assert payload["buckets"] == []
    assert payload["latest_activity_at"] == newest.created_at
    # The grouped query, then one read of the last day. Not the whole history.
    assert len(database.queries) == 2
    assert "GROUP BY bucket" in database.queries[0]
    assert "ORDER BY created_at DESC" in database.queries[1]
    since = dt.datetime.fromisoformat(database.params[1]["start_at"].replace("Z", "+00:00"))
    age = dt.datetime.now(dt.UTC) - since
    assert dt.timedelta(days=1) <= age < dt.timedelta(days=1, minutes=1)


def test_console_activity_page_lists_the_newest_rows_from_a_bounded_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id = _signed_in_client("usage-list-busy@example.com")
    database = ActivityClickHouse()
    rows = [
        _recent(workspace_id, index, dt.timedelta(minutes=index + 1), model=f"model-{index:03d}")
        for index in range(60)
    ]
    rows.append(_recent(workspace_id, 60, dt.timedelta(days=200), model="model-ancient"))
    for row in rows:
        database.add(row)
    _route_through_clickhouse(monkeypatch, database)

    response = client.get("/console/activity")

    assert response.status_code == 200
    listed = response.text.split("<h2>Recent activity</h2>", 1)[1]
    shown = [row for row in rows if f"<td>{row.model}</td>" in listed]
    assert shown == rows[:50]
    assert [listed.index(f"<td>{row.model}</td>") for row in shown] == sorted(
        listed.index(f"<td>{row.model}</td>") for row in shown
    )
    # One read, of the last day only.
    [params] = database.params
    assert params["limit"] == "51"
    since = dt.datetime.fromisoformat(params["start_at"].replace("Z", "+00:00"))
    assert dt.timedelta(days=1) <= dt.datetime.now(dt.UTC) - since < dt.timedelta(days=1, minutes=1)


def test_console_activity_page_still_lists_a_quiet_workspace_whole_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id = _signed_in_client("usage-list-quiet@example.com")
    database = ActivityClickHouse()
    rows = [
        _recent(workspace_id, 0, dt.timedelta(hours=2), model="model-today"),
        _recent(workspace_id, 1, dt.timedelta(days=12), model="model-this-month"),
        _recent(workspace_id, 2, dt.timedelta(days=200), model="model-long-ago"),
    ]
    for row in rows:
        database.add(row)
    _route_through_clickhouse(monkeypatch, database)

    response = client.get("/console/activity")

    assert response.status_code == 200
    listed = response.text.split("<h2>Recent activity</h2>", 1)[1]
    assert [listed.index(f"<td>{row.model}</td>") for row in rows] == sorted(
        listed.index(f"<td>{row.model}</td>") for row in rows
    )
    # Fewer rows than the page shows: the read widens to the whole history,
    # so what the page lists is what it listed before the windows.
    assert [params.get("start_at") is not None for params in database.params] == [
        True,
        True,
        False,
    ]


def test_console_activity_page_remembers_range_cancels_and_offers_retry() -> None:
    client, _workspace_id = _signed_in_client("usage-page-hooks@example.com")

    page = client.get("/console/activity").text

    # A visible loading state, and an error state with a retry control.
    assert 'data-usage-loading role="status" aria-live="polite">' in page
    assert "<p>Loading usage…</p>" in page
    assert 'data-usage-error role="alert" hidden>' in page
    assert '<button class="btn" type="button" data-usage-retry>Retry</button>' in page
    assert 'panel.querySelector("[data-usage-retry]").addEventListener("click", loadUsage);' in page
    # The remembered range: read and written inside try/catch, restored before
    # the first request so the page loads one range, not two.
    assert 'const RANGE_STORAGE_KEY = "tr.console.activity.usage-range";' in page
    assert "window.localStorage.getItem(RANGE_STORAGE_KEY)" in page
    assert "window.localStorage.setItem(RANGE_STORAGE_KEY, range)" in page
    assert page.index("restoreRange();\n    loadUsage();") > 0
    # A superseded request is cancelled.
    assert "inFlight.abort();" in page
    assert "signal: inFlight ? inFlight.signal : undefined" in page


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
